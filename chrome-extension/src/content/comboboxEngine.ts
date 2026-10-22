/**
 * ARIA combobox / listbox filler.
 *
 * Custom dropdowns (react-select, Headless UI, Radix, Workday button-listboxes…)
 * cannot be driven by setting `.value`: the chosen value lives in the widget's
 * own state and only commits when the user opens the popup and clicks an option.
 * This module mimics that interaction, open → find the listbox → click the
 * matching option → confirm, following the WAI-ARIA combobox/listbox pattern.
 *
 * It is async and ONE-SHOT on purpose: re-driving a dropdown on every page
 * mutation is exactly the focus-stealing churn the reconciler avoids, so these
 * controls are filled once during the autofill pass and never drift-tracked.
 */
import {
  boxPieces,
  cleanText,
  deepQueryAll,
  dispatchInputEvents,
  isPlaceholderFiller,
  setNativeValue,
} from "./domUtils";
import { normalize } from "./fieldMatcher";
import { matchOption } from "./writeEngine";
import { looksLikePlaces, pickPlaceOption } from "./placeMatch";
import { snapSchool } from "./schoolMatch";
import { regionFromText } from "./geo";
import {
  MULTISELECT_CONTAINER_FRAGMENT as WD_MULTISELECT_CONTAINER_FRAGMENT,
  SEARCH_BOX_FRAGMENT as WD_SEARCH_BOX_FRAGMENT,
  SELECTED_ITEM_FRAGMENT as WD_SELECTED_ITEM_FRAGMENT,
} from "./adapters/workdaySelectors";

export interface ComboboxResult {
  filled: boolean;
  reason?: string;
  /** On a no-match failure: the open listbox's REAL option labels, harvested
   *  for the one-shot AI re-ask pass (contentScript). */
  options?: string[];
}

export interface FillComboboxOptions {
  /** Injectable for tests: defaults to a real setTimeout-based sleep. */
  sleep?: (ms: number) => Promise<void>;
  /** How long to wait for the menu to mount after opening. */
  openWaitMs?: number;
  /** How long to wait for the selection to commit after clicking. */
  commitWaitMs?: number;
  /** Poll interval while waiting. */
  pollMs?: number;
  /** Force multi-select handling (value split into one chip per item). Also
   *  auto-detected from aria-multiselectable / react-select "is-multi". */
  multi?: boolean;
  /** For a city/location field: the applicant's full place ("Toronto, ON,
   *  Canada"). When the options read as places, the one that IS that place is
   *  chosen (placeMatch.ts), never one that merely shares the city name. */
  placeHint?: string;
  /** For a school field: an option is chosen by the school's own words
   *  (schoolMatch.ts), never by a word it shares. Everpure's search answered
   *  the full name with other schools of Austin, and "Austin Peay State
   *  University" was taken for "The University of Texas at Austin"
   *  (Greenhouse, live 2026-10-17). */
  school?: boolean;
  /** An input whose own text is an answer (a street address): when no
   *  suggestion is the value, the typed value stays and counts. Paylocity's
   *  "Address Line 1" suggests nothing for a UK street (live 2026-10-03). */
  freeText?: boolean;
}

const DEFAULTS = { openWaitMs: 1500, commitWaitMs: 2500, pollMs: 50 };

/**
 * True when an element is an ARIA combobox/listbox we can drive by clicking an
 * option, as opposed to a free-text field. We require an explicit listbox
 * affordance so plain inputs are never mistaken for dropdowns.
 */
export function isAriaCombobox(el: HTMLElement): boolean {
  const role = (el.getAttribute("role") || "").toLowerCase();
  const haspopup = (el.getAttribute("aria-haspopup") || "").toLowerCase();
  if (haspopup === "listbox") return true;
  if (role === "combobox") {
    // A combobox that actually toggles a listbox declares its state/target.
    return (
      el.hasAttribute("aria-expanded") ||
      el.hasAttribute("aria-controls") ||
      el.hasAttribute("aria-owns")
    );
  }
  return false;
}

/** Detect a multi-select combobox (react-select "is-multi", or an
 *  aria-multiselectable trigger/listbox), its value is a list, added one chip
 *  at a time rather than matched as one option. */
function isMultiSelect(trigger: HTMLElement): boolean {
  if (trigger.getAttribute("aria-multiselectable") === "true") return true;
  const ids = `${trigger.getAttribute("aria-controls") ?? ""} ${trigger.getAttribute("aria-owns") ?? ""}`.trim();
  for (const id of ids.split(/\s+/).filter(Boolean)) {
    if (byIdNear(trigger, id)?.getAttribute("aria-multiselectable") === "true") return true;
  }
  // NB Workday's `data-uxi-multiselect-id` on the input is NOT a multi signal and
  // must not be added here: it sits on single-value prompts too (Country Phone
  // Code, a degree, see test/fixtures/workdayReal.ts), where every pick REPLACES
  // the last, so splitting "Toronto, Ontario" commits "Ontario" and reports
  // success. Real Workday multiselects carry the container below anyway.
  //
  // Workday's multiselect ("Type to Add Skills") exposes no ARIA multi signal,
  // its container automation-id is the only reliable marker.
  return Boolean(
    trigger.closest(
      '[class*="is-multi" i], [class*="multiselect" i], ' +
        `[data-automation-id*="${WD_MULTISELECT_CONTAINER_FRAGMENT}" i]`
    )
  );
}

/** Split a multi-value answer into distinct items (never applied to a
 *  single-select, a single answer may legitimately contain a comma). Capped so
 *  a 30-skill profile can't turn one field into a minutes-long type/select loop. */
function splitMultiValue(value: string): string[] {
  return [...new Set(value.split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean))].slice(0, 12);
}

/**
 * Fill an ARIA combobox. Single-select by default; for a multi-select widget
 * (skills, multiple locations) the value is split and each item is added as its
 * own chip via a type→select cycle.
 */
export async function fillAriaCombobox(
  trigger: HTMLElement,
  value: string,
  opts: FillComboboxOptions = {}
): Promise<ComboboxResult> {
  if (!trigger.isConnected) return { filled: false, reason: "Field was removed. Rescan the page" };
  const multi = opts.multi || isMultiSelect(trigger);
  const parts = multi ? splitMultiValue(value) : [value];
  if (parts.length <= 1) return selectOne(trigger, parts[0] ?? value, opts);

  let anyFilled = false;
  const missing: string[] = [];
  let lastOptions: string[] | undefined;
  for (const part of parts) {
    const r = await selectOne(trigger, part, opts);
    if (r.filled) anyFilled = true;
    else {
      missing.push(part);
      if (r.options) lastOptions = r.options;
    }
  }
  if (anyFilled) {
    return { filled: true, reason: missing.length ? `Added; couldn't match: ${missing.join(", ")}` : undefined };
  }
  return { filled: false, reason: `No option matches "${truncate(value)}"`, options: lastOptions };
}

async function selectOne(
  trigger: HTMLElement,
  value: string,
  opts: FillComboboxOptions = {}
): Promise<ComboboxResult> {
  const sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
  const openWaitMs = opts.openWaitMs ?? DEFAULTS.openWaitMs;
  const commitWaitMs = opts.commitWaitMs ?? DEFAULTS.commitWaitMs;
  const pollMs = opts.pollMs ?? DEFAULTS.pollMs;

  if (!trigger.isConnected) {
    return { filled: false, reason: "Field was removed. Rescan the page" };
  }
  // Already showing the desired value, idempotent no-op, never opens the menu.
  if (comboboxShowsValue(trigger, value)) return { filled: true };

  // Anything we type into a typeahead is OURS to clean up: on any failure the
  // input must go back to what it held before, so a filter string ("Quebec",
  // a comma-joined skills list) is never left sitting in the field looking
  // like an entered answer.
  // A box around its own search input (Paylocity's select, live 2026-10-05)
  // is searched through that input.
  const input =
    trigger instanceof HTMLInputElement
      ? trigger
      : trigger.querySelector<HTMLInputElement>('input[aria-autocomplete="list"], input[aria-autocomplete="both"]');
  const originalText = input?.value ?? "";
  let lastTyped: string | null = null;
  const restoreTyped = (): void => {
    if (input && input.value !== originalText) typeInto(input, originalText);
  };

  open(trigger);

  // Wait for the listbox to mount. Typeahead inputs may need the value typed in
  // to surface (and filter, or async-load) the options.
  let listbox = await waitFor(() => getListbox(trigger), sleep, openWaitMs, pollMs);
  // SF's paginated picklists occasionally swallow the first activation (the
  // listbox never mounts). Re-open once, open() re-activates only while the
  // widget is still collapsed, so this is a no-op when it did open.
  if (!listbox) {
    open(trigger);
    listbox = await waitFor(() => getListbox(trigger), sleep, openWaitMs, pollMs);
  }
  let option = listbox ? findOption(listbox, value, opts.placeHint, opts.school) : null;
  if (input && isTypeahead(input) && !option) {
    // Progressively broader filter texts: the full answer; the answer as a
    // search list spells it, without accents or a leading "The"; its leading
    // words, which find the list's own wording; its first word (a
    // literal-substring search finds "TypeScript" but not "TypeScript /
    // JavaScript"); then no filter at all (a long answer can over-filter a
    // substring-matching widget down to zero options, "I am not a protected
    // veteran" never substring-matches "No, I am not a veteran"). Greenhouse's
    // school search found nothing for "San José State University" or "The
    // University of Texas at Austin": its list says "San Jose State University"
    // and "University of Texas - Austin", which "University of Texas" finds
    // (Twitch, SpaceX, live 2026-10-03).
    const plain = value.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/^\s*the\s+/i, "").trim();
    const words = plain.split(/[\s/,;]+/).filter(Boolean);
    const firstWord = words[0] ?? "";
    const attempts: string[] = [];
    const add = (text: string): void => {
      if (!attempts.some((a) => a.toLowerCase() === text.trim().toLowerCase())) attempts.push(text.trim());
    };
    add(value);
    add(plain);
    // A state or province is searched by its code too, and a code by its
    // name: Paylocity's State list filters codes by prefix, and "Texas"
    // found nothing there (live 2026-10-08).
    const region = regionFromText(value);
    if (region) {
      add(region.code);
      add(region.name);
    }
    if (words.length >= 4) add(leadingWords(words));
    if (firstWord.length >= 3) add(firstWord);
    attempts.push("");
    for (const text of attempts) {
      const preTypeKey = optionsKey(getListbox(trigger));
      lastTyped = text;
      typeInto(input, text);
      const hit = await pollForMatch(trigger, value, text, preTypeKey, sleep, openWaitMs, pollMs, opts.placeHint, opts.school);
      if (hit) {
        listbox = hit.lb;
        option = hit.opt;
        break;
      }
      listbox = getListbox(trigger) ?? listbox;
    }
  }
  // A street address is an answer as typed: no suggestion that is it (none
  // at all, or other places) leaves the street in the box.
  if (!option && opts.freeText && input) {
    close(trigger);
    if (input.value !== value) typeInto(input, value);
    return input.value === value ? { filled: true } : { filled: false, reason: "The address box did not keep the street. Type it manually" };
  }
  if (!listbox) {
    restoreTyped();
    close(trigger);
    return { filled: false, reason: `Couldn't open the "${truncate(value)}" dropdown. Select it manually` };
  }

  if (!option) {
    const options = optionLabels(listbox);
    restoreTyped();
    close(trigger);
    // Record what the listbox actually offered, distinguishes an async/empty
    // read from a contaminated (wrong-field) menu from a genuine match miss.
    const seen =
      options && options.length ? ` (saw: ${options.slice(0, 6).join(" | ")})` : " (listbox had no options)";
    return {
      filled: false,
      reason: `No option matches "${truncate(value)}"${seen}`,
      options,
    };
  }

  const chosenText = optionText(option);
  clickOption(option);

  // Confirm: the committed value must actually SHOW on the widget. A merely
  // collapsed popup is not proof, a click that lands nowhere also leaves the
  // trigger collapsed, and reporting that as filled hides a real failure from
  // the user (the empty-required-dropdown bug). The widget displays the OPTION's
  // text, which for a fuzzy match ("I am not a protected veteran" → "No, I am
  // not a veteran") differs from the target, accept either. One more trap: the
  // input echoing back exactly what WE typed proves nothing (a click that lands
  // nowhere leaves our filter text sitting there), that echo only counts when
  // the menu's dismissal corroborates that the selection landed.
  const committed = await waitFor(
    () => {
      if (
        comboboxShowsValue(trigger, value, lastTyped) ||
        (chosenText && comboboxShowsValue(trigger, chosenText, lastTyped))
      ) {
        return true;
      }
      if (
        input &&
        input.value &&
        input.value === lastTyped &&
        (textMatches(input.value, value) || (chosenText !== "" && textMatches(input.value, chosenText)))
      ) {
        const lb = getListbox(trigger);
        if (!lb || !isVisible(lb) || trigger.getAttribute("aria-expanded") === "false") return true;
      }
      return null;
    },
    sleep,
    commitWaitMs,
    pollMs
  );
  if (!committed) {
    restoreTyped();
    return { filled: false, reason: "Selection didn't stick. Select it manually" };
  }
  return { filled: true };
}

// ---------------------------------------------------------------------------
// Interaction primitives
// ---------------------------------------------------------------------------

/** A realistic activation sequence: pointer + mouse + click. Exported for the
 *  flow controller's advance-button click (advance.ts). */
export function activateElement(el: HTMLElement): void {
  const rect = el.getBoundingClientRect();
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  // composed: true is essential, many ATS (Workday included) render the app in
  // an open shadow root, and frameworks delegate their click handling at the
  // document. A non-composed event bubbles only within the shadow tree, so the
  // button's real handler never runs and the page never advances. Real user
  // events are composed; coordinates + button/buttons make the sequence faithful
  // for handlers that inspect them. (No `view`: jsdom rejects it and browsers
  // don't need it here.)
  const mouse = (type: string, buttons: number): MouseEvent =>
    new MouseEvent(type, {
      bubbles: true, cancelable: true, composed: true,
      button: 0, buttons, clientX: cx, clientY: cy,
    });
  try {
    el.focus({ preventScroll: true }); // some handlers gate on focus first
  } catch {
    // not focusable, the pointer/mouse sequence below still activates it
  }
  firePointer(el, "pointerdown", cx, cy);
  el.dispatchEvent(mouse("mousedown", 1));
  firePointer(el, "pointerup", cx, cy);
  el.dispatchEvent(mouse("mouseup", 0));
  el.dispatchEvent(mouse("click", 0));
}

function firePointer(el: HTMLElement, type: string, cx = 0, cy = 0): void {
  const PE = (el.ownerDocument.defaultView as unknown as { PointerEvent?: typeof PointerEvent })?.PointerEvent;
  if (!PE) return;
  try {
    el.dispatchEvent(
      new PE(type, {
        bubbles: true, cancelable: true, composed: true,
        clientX: cx, clientY: cy, isPrimary: true, pointerType: "mouse",
      })
    );
  } catch {
    // jsdom rejects some PointerEvent inits, ignore; mouse events cover it.
  }
}

function open(trigger: HTMLElement): void {
  trigger.focus({ preventScroll: true });
  if (trigger.getAttribute("aria-expanded") === "true") return;
  activateElement(trigger);
}

function close(trigger: HTMLElement): void {
  trigger.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true, cancelable: true })
  );
  // Many widgets dismiss the popup on a pointer-down outside it rather than on
  // Escape: fire one on <body> too. (Never re-activate the trigger: that would
  // re-open a widget that opens, rather than toggles, on click.)
  const body = trigger.ownerDocument.body;
  if (body && trigger.getAttribute("aria-expanded") === "true") {
    body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    body.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
  }
}

function clickOption(option: HTMLElement): void {
  option.scrollIntoView?.({ block: "nearest" });
  activateElement(option);
}

function isTypeahead(trigger: HTMLElement): boolean {
  if (!(trigger instanceof HTMLInputElement)) return false;
  // Workday's searchBox has NO aria-autocomplete and an empty listbox until
  // text is typed, opening it and reading options finds nothing.
  const testId = trigger.getAttribute("data-automation-id") ?? "";
  if (testId.toLowerCase().includes(WD_SEARCH_BOX_FRAGMENT.toLowerCase())) return true;
  const ac = (trigger.getAttribute("aria-autocomplete") || "").toLowerCase();
  return ac === "list" || ac === "both" || ac === "inline" || trigger.type === "text";
}

const MINOR_WORDS = /^(of|the|at|and|&|for|in|on|de|du|des|la|le|-)$/i;

/** The words up to and including the second that names something:
 *  "University of Texas" of "University of Texas at Austin". */
function leadingWords(words: string[]): string {
  let named = 0;
  const out: string[] = [];
  for (const w of words) {
    out.push(w);
    if (!MINOR_WORDS.test(w) && ++named === 2) break;
  }
  return out.join(" ");
}

/** Cheap identity of a listbox's current options, change/settle detection. */
function optionsKey(listbox: HTMLElement | null): string {
  if (!listbox) return "";
  return optionsIn(listbox)
    .map((o) => optionText(o))
    .join("\u0000");
}

/**
 * After typing a filter, poll for a MATCH, but stop as soon as the widget has
 * given its final answer for this text. Async widgets (Workday's skills search,
 * SF's paginated picklists) repopulate a beat after the input event, so we keep
 * polling while the option list is still CHANGING; once it has reacted and gone
 * stable, or never reacts within the reaction window (a widget that doesn't
 * filter as you type), burning the rest of the budget is pure user-visible
 * dead time: the field sits there with our filter text looking half-filled.
 */
async function pollForMatch(
  trigger: HTMLElement,
  target: string,
  typed: string,
  preTypeKey: string,
  sleep: (ms: number) => Promise<void>,
  budgetMs: number,
  pollMs: number,
  placeHint?: string,
  school?: boolean
): Promise<{ lb: HTMLElement; opt: HTMLElement } | null> {
  const reactionWindowMs = Math.min(budgetMs, 800);
  let lastKey = preTypeKey;
  let reacted = false;
  let stablePolls = 0;
  for (let elapsed = 0; ; elapsed += pollMs) {
    const lb = getListbox(trigger);
    const opt = lb ? findOption(lb, target, placeHint, school) : null;
    if (lb && opt) return { lb, opt };
    const key = optionsKey(lb);
    // An EMPTY option list is the widget mid-flight, never its answer.
    //
    // A remote typeahead (Greenhouse's school search, Workday's skills search)
    // renders nothing until it has a response, and shows a "Loading…" notice
    // that is not a [role="option"], so `key` stays empty across the whole
    // round trip. Both of the exits below used to fire on that empty list: the
    // stability counter three polls after it emptied, and the reaction window
    // 800ms after typing when it had been empty all along. Either one ends the
    // attempt while the request is still in the air, and the caller then
    // restores the input to blank. That is what the user saw as School being
    // filled in and then removed (autofill_reports #168, 2026-08-13).
    //
    // So neither exit may end the wait while the list is empty; only the hard
    // budget can. A list with something in it can still settle immediately,
    // which keeps the fast path fast for ordinary local dropdowns.
    const hasOptions = key.length > 0;
    if (key !== lastKey) {
      reacted = true;
      stablePolls = 0;
      lastKey = key;
    } else if (reacted && hasOptions && answersTyped(lb, typed) && ++stablePolls >= 3) {
      return null; // the filter answered and settled, no match in its final list
    }
    if (!reacted && hasOptions && elapsed >= reactionWindowMs) return null;
    // A place search still empty gets longer: Rippling's Location asks Google
    // Places, whose script loads on first use, and no suggestion came within
    // the usual wait (live 2026-10-05: the field was left empty).
    if (elapsed >= (placeHint && !hasOptions ? Math.max(budgetMs, PLACE_SEARCH_WAIT_MS) : budgetMs)) return null;
    await sleep(pollMs);
  }
}

/** How long a place search may stay empty before an attempt gives up. */
const PLACE_SEARCH_WAIT_MS = 4000;

/**
 * Whether a changed list is the filter's answer to `typed`: an option carries
 * one of its words (or nothing was typed). A list that changes for another
 * reason is not, and settling on it gave up before the filter ran: Rippling's
 * dial-code picker first scrolls its virtual list to the current selection and
 * filters only after a half-second debounce (live 2026-10-05).
 */
function answersTyped(listbox: HTMLElement | null, typed: string): boolean {
  const words = normalize(typed).split(" ").filter((w) => w.length >= 2);
  if (words.length === 0 || !listbox) return true;
  return optionsIn(listbox).some((o) => {
    const text = normalize(optionText(o));
    return words.some((w) => text.includes(w));
  });
}

function typeInto(input: HTMLInputElement, value: string): void {
  // Bracket the value change with key events: legacy widgets (SAP
  // SuccessFactors picklists) refresh their suggestion list on keyup, not on
  // the input event, so without these the filter text lands but never filters.
  const key = value ? value[value.length - 1] : "Backspace";
  const init: KeyboardEventInit = { key, bubbles: true, cancelable: true, composed: true };
  input.dispatchEvent(new KeyboardEvent("keydown", init));
  setNativeValue(input, value);
  dispatchInputEvents(input, value);
  input.dispatchEvent(new KeyboardEvent("keyup", init));
}

// ---------------------------------------------------------------------------
// Listbox + option lookup
// ---------------------------------------------------------------------------

/**
 * Resolve an IDREF (aria-controls / aria-owns / aria-activedescendant) the way
 * the accessibility tree does for web components: in the trigger's own root
 * first, then each enclosing shadow root, then the document.
 * document.getElementById cannot see into a shadow root, and SmartRecruiters'
 * City autocomplete keeps its menu in the PARENT component's shadow root, so
 * the menu was never found and City stayed empty (live, 2026-10-03).
 */
function byIdNear(el: HTMLElement, id: string): HTMLElement | null {
  let node: Node | null = el;
  const seen = new Set<Node>();
  while (node) {
    const root = node.getRootNode() as Document | ShadowRoot;
    if (seen.has(root)) break;
    seen.add(root);
    // Inside a component, the target can sit in a SIBLING component's shadow
    // root (spl-autocomplete holds spl-input and spl-dropdown, each with its own
    // root), so a shadow level is searched deeply; the document level stays a
    // cheap getElementById (this runs on every poll while a menu opens).
    const hit =
      root instanceof ShadowRoot
        ? (root.getElementById?.(id) ??
          Array.from(root.querySelectorAll("[id]")).find((e) => e.id === id) ??
          deepQueryAll(root, "[id]").find((e) => e.id === id) ??
          null)
        : (root as Document).getElementById(id);
    if (hit) return hit as HTMLElement;
    node = root instanceof ShadowRoot ? root.host : null;
  }
  return null;
}

/**
 * Every option of a listbox, including options PROJECTED into it through a
 * <slot>. A web-component menu (SmartRecruiters' spl-dropdown) is a shadow-DOM
 * listbox whose items are the host's light-DOM children, slotted in: they are
 * not descendants of the listbox element, so a plain subtree query found none
 * ("listbox had no options", live 2026-10-03). Each slotted item may itself
 * keep its role=option node in its own shadow root.
 */
function optionsIn(listbox: HTMLElement): HTMLElement[] {
  const out = new Set<HTMLElement>(deepQueryAll(listbox, '[role="option"]'));
  for (const slot of deepQueryAll(listbox, "slot") as HTMLSlotElement[]) {
    for (const assigned of slot.assignedElements({ flatten: true }) as HTMLElement[]) {
      if (assigned.matches('[role="option"]')) out.add(assigned);
      for (const o of deepQueryAll(assigned, '[role="option"]')) out.add(o);
      if (assigned.shadowRoot) for (const o of deepQueryAll(assigned.shadowRoot, '[role="option"]')) out.add(o);
    }
  }
  // A menu with no ARIA roles at all, which getListbox only accepts from a
  // container the trigger names itself.
  if (out.size === 0 && listbox.getAttribute("role") !== "listbox") return rolelessRows(listbox);
  return [...out];
}

/** What a list shows in place of options. */
const NO_RESULTS = /^(no (results|options|matches|items)( found)?|nothing found|loading)\W*$/i;

/**
 * The rows of a menu that has no ARIA roles. Paylocity's select names its
 * menu with aria-owns, and the menu is a virtualized list of plain
 * div.ListItemEven / div.ListItemOdd rows (live 2026-10-08): the engine saw
 * no listbox and reported the State dropdown as one it could not open. The
 * rows are the innermost elements whose class says item or option, with text.
 */
function rolelessRows(container: HTMLElement): HTMLElement[] {
  if (container.querySelector('[role="option"], [role="listbox"]')) return [];
  const ROW = '[class*="item" i], [class*="option" i]';
  return Array.from(container.querySelectorAll<HTMLElement>(ROW)).filter((el) => {
    if (el.querySelector(ROW)) return false;
    const text = cleanText(el.textContent);
    return text !== "" && !NO_RESULTS.test(text);
  });
}

/** Locate the open listbox: prefer the one the combobox points at (it may be
 *  portaled far away in the DOM), else a visible listbox that isn't part of a
 *  DIFFERENT widget. */
function getListbox(trigger: HTMLElement): HTMLElement | null {
  const doc = trigger.ownerDocument;
  const declared = `${trigger.getAttribute("aria-controls") ?? ""} ${trigger.getAttribute("aria-owns") ?? ""}`
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  for (const id of declared) {
    const el = byIdNear(trigger, id);
    if (!el) continue;
    const lb = (el.getAttribute("role") === "listbox" ? el : el.querySelector('[role="listbox"]')) as HTMLElement | null;
    if (lb && isVisible(lb) && hasOptions(lb)) return lb;
    // A named menu with no ARIA roles (Paylocity's): its rows are the options.
    if (!lb && isVisible(el) && rolelessRows(el).length > 0) return el;
  }
  // A trigger that NAMES its listbox (aria-owns / aria-controls) must only ever
  // use THAT listbox, never a neighbour's. SAP SuccessFactors renders each
  // picklist's options into its own aria-owns'd <ul> a beat after opening;
  // falling back to "a visible listbox" here grabbed the previously-opened
  // field's still-open menu (race read gender's options, gender read the city
  // field's). Return null so waitFor keeps polling for the declared one.
  if (declared.length > 0) return null;
  // Fallback for widgets that declare no association. Prefer the menu inside the
  // trigger's OWN widget container (its own popup). Only then trust a document-
  // wide portaled menu, and ONLY when it is UNAMBIGUOUS (exactly one open):
  // grabbing "the first visible listbox" lets a stale or neighbouring dropdown's
  // menu contaminate this field's options (the mixed-up-options bug). A wrong
  // option list is worse than none, so when it's ambiguous we return null.
  const container = trigger.closest('[class*="select" i], [class*="combobox" i]');
  if (container) {
    const own = deepQueryAll(container, '[role="listbox"]').find((lb) => isVisible(lb) && hasOptions(lb));
    if (own) return own;
  }
  const visible = deepQueryAll(doc, '[role="listbox"]').filter(
    (lb) => isVisible(lb) && hasOptions(lb) && !belongsToOtherWidget(lb, trigger)
  );
  return visible.length === 1 ? visible[0] : null;
}

/** True when `lb` sits inside a widget wrapper that hosts a combobox trigger
 *  other than `trigger`: i.e. it's some other control's menu. */
function belongsToOtherWidget(lb: HTMLElement, trigger: HTMLElement): boolean {
  let node: HTMLElement | null = lb.parentElement;
  for (let hops = 0; node && node !== node.ownerDocument.body && hops < 8; hops++, node = node.parentElement) {
    if (node.contains(trigger)) return false; // shared container, it's ours
    const owner = node.querySelector('[role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="dialog"]');
    if (owner && owner !== trigger) return true;
  }
  return false;
}

function hasOptions(listbox: HTMLElement): boolean {
  return optionsIn(listbox).length > 0;
}

function findOption(listbox: HTMLElement, value: string, placeHint?: string, school?: boolean): HTMLElement | null {
  const options = optionsIn(listbox).filter(
    (o) => o.getAttribute("aria-disabled") !== "true"
  );
  // A school by its own words: none of them, no option (the search goes on).
  // An option is read by its first box too, where a result shows the name
  // before its country and domain (Ashby).
  if (school) {
    const texts: string[] = [];
    const owners: HTMLElement[] = [];
    for (const o of options) {
      for (const t of new Set([boxPieces(o)[0] ?? "", optionText(o)])) {
        if (!t) continue;
        texts.push(t);
        owners.push(o);
      }
    }
    const hit = snapSchool(texts, value);
    return hit === null ? null : owners[texts.indexOf(hit)];
  }
  // Place suggestions are chosen as PLACES: token matching would read
  // "Toronto, OH, US" as a perfect match for "Toronto, ON, Canada".
  if (placeHint) {
    const texts = options.map((o) => optionText(o));
    if (looksLikePlaces(texts)) {
      const i = pickPlaceOption(texts, placeHint);
      return i >= 0 ? options[i] : null;
    }
  }
  const hit = matchOption(
    options,
    (o) => optionText(o),
    (o) => o.getAttribute("data-value") ?? o.getAttribute("value") ?? "",
    value
  );
  if (hit) return hit;
  // A state or province by name or code: lists hold either ("TX" on
  // Paylocity, "Texas" elsewhere), and the answer is one of the two.
  const wanted = regionFromText(value);
  if (!wanted) return null;
  const same = options.filter((o) => {
    const r = regionFromText(optionText(o), wanted.country);
    return r !== null && r.code === wanted.code && r.country === wanted.country;
  });
  return same.length === 1 ? same[0] : null;
}

/** Visible label of an option, ignoring nested check/icon glyph text. */
function optionText(option: HTMLElement): string {
  const labelled = option.getAttribute("aria-label");
  return cleanText(labelled) || cleanText(option.textContent) || cleanText(flatText(option));
}

/**
 * The text a user SEES in `el`, following <slot>s to the nodes projected into
 * them. A web-component option (SmartRecruiters' spl-dropdown-item) is a
 * `div[role=option]` in a shadow root whose label is the HOST's light-DOM text,
 * projected through a slot, so its own textContent is empty.
 */
function flatText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (node instanceof HTMLSlotElement) {
    const assigned = node.assignedNodes({ flatten: true });
    // An unfilled slot renders its fallback content.
    return (assigned.length ? assigned : Array.from(node.childNodes)).map(flatText).join(" ");
  }
  if (node instanceof Element) {
    if (/^(STYLE|SCRIPT|TEMPLATE)$/.test(node.tagName)) return "";
    // A tooltip repeats what it annotates: SmartRecruiters' items keep their
    // text in <spl-truncate>, whose <spl-tooltip> holds it again, and every
    // option read as its label twice ("WA WA"; Intuitive, live, round 8).
    if (node.getAttribute("role") === "tooltip") return "";
    // A host renders its SHADOW tree (whose slots pull the light children
    // in); walking both would count projected text twice.
    const shadow = (node as HTMLElement).shadowRoot;
    return Array.from((shadow ?? node).childNodes).map(flatText).join(" ");
  }
  return "";
}

/**
 * Whether the widget already DISPLAYS `value` as its committed selection.
 * Callers use it to skip re-driving an already-filled dropdown: re-driving a
 * react-select types into it, which visually ERASES the committed value and
 * re-picks it, the fill/remove/fill churn users see when a later pass
 * re-targets a field an earlier pass already filled.
 */
export function comboboxDisplaysValue(trigger: HTMLElement, value: string): boolean {
  return comboboxShowsValue(trigger, value);
}

/**
 * Read a combobox's option labels WITHOUT opening it, only when the listbox is
 * already mounted in the DOM. Many widgets keep a hidden listbox; react-select
 * mounts it lazily on open, so this returns undefined there (the AI then answers
 * from the label alone). Visibility is ignored on purpose: a mounted-but-hidden
 * listbox is a valid source.
 */
export function readComboboxOptions(trigger: HTMLElement, limit = 60): string[] | undefined {
  const listbox = findMountedListbox(trigger);
  if (!listbox) return undefined;
  // A virtualized list renders a window of its options, each telling the
  // whole list's size: the window is not the list. Read as one, an answer
  // outside it was "not offered" (Rippling's 245 dialing codes, 7 rendered,
  // live 2026-10-05). Unknown options are typed to filter at fill time.
  const rendered = optionsIn(listbox);
  const setSize = Math.max(0, ...rendered.map((o) => Number(o.getAttribute("aria-setsize")) || 0));
  if (setSize > rendered.length) return undefined;
  return optionLabels(listbox, limit);
}

/**
 * Actively harvest a lazy combobox's options by briefly opening its menu and
 * closing it again, for widgets (react-select…) that mount the listbox only
 * while open, where readComboboxOptions has nothing to read. Used to give the
 * missing-info modal the REAL choices for a dropdown nothing could answer.
 * Best-effort: returns undefined when no listbox appears; always closes what
 * it opened, and never types or selects anything.
 */
export async function harvestComboboxOptions(
  trigger: HTMLElement,
  opts: FillComboboxOptions = {}
): Promise<string[] | undefined> {
  const mounted = readComboboxOptions(trigger);
  if (mounted && mounted.length > 0) return mounted;
  if (!trigger.isConnected) return undefined;

  const sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
  const openWaitMs = opts.openWaitMs ?? DEFAULTS.openWaitMs;
  const pollMs = opts.pollMs ?? DEFAULTS.pollMs;

  open(trigger);
  const listbox = await waitFor(() => getListbox(trigger), sleep, openWaitMs, pollMs);
  const labels = listbox ? optionLabels(listbox) : undefined;
  close(trigger);
  return labels;
}

/** Non-disabled option labels of a listbox, trimmed for transport (cap 60;
 *  resolution asks for every one, see scanPage). */
function optionLabels(listbox: HTMLElement, limit = 60): string[] | undefined {
  const labels = optionsIn(listbox)
    .filter((o) => o.getAttribute("aria-disabled") !== "true")
    .map((o) => optionText(o))
    .filter((t) => t.length > 0)
    .slice(0, limit);
  return labels.length > 0 ? labels : undefined;
}

/** The combobox's listbox if it is already in the DOM (no opening, any visibility). */
function findMountedListbox(trigger: HTMLElement): HTMLElement | null {
  const ids = `${trigger.getAttribute("aria-controls") ?? ""} ${trigger.getAttribute("aria-owns") ?? ""}`.trim();
  for (const id of ids.split(/\s+/).filter(Boolean)) {
    const el = byIdNear(trigger, id);
    if (!el) continue;
    const lb = (el.getAttribute("role") === "listbox" ? el : el.querySelector('[role="listbox"]')) as HTMLElement | null;
    if (lb && hasOptions(lb)) return lb;
  }
  // Same-container fallback: a listbox rendered next to the trigger (not a
  // document-wide search, which could grab an unrelated open menu at scan time).
  // [role="combobox"] is intentionally excluded, the trigger is often that
  // element itself, and closest() would then return it (no listbox descendant).
  const container =
    trigger.closest('[class*="select" i], [class*="combobox" i]') ?? trigger.parentElement;
  const lb = container?.querySelector('[role="listbox"]') as HTMLElement | null;
  return lb && hasOptions(lb) ? lb : null;
}

/**
 * The combobox's currently-displayed value, if one is committed, best-effort,
 * for scan-time "already answered?" detection.
 *
 * A button-style trigger (Workday's prompt) displays its selection AS its own
 * text and carries none of the stronger signals, so reading only those reported
 * every such widget as empty however it had been answered. Three things believe
 * this: the gap modal re-asked questions the page had already answered, the
 * flow's `hasUnfilledRequired` gate could never clear on Workday, and a later
 * pass would overwrite an answer the user had picked by hand.
 *
 * It is read LAST and only from a button-like trigger, so an input-based widget
 * (react-select) is unaffected; and "Select One" is placeholder filler, so an
 * unset prompt still reads as empty, which is what keeps this from silently
 * suppressing the fill of a field nobody has answered.
 */
export function readComboboxValue(trigger: HTMLElement): string | undefined {
  const ownText = isButtonLikeTrigger(trigger)
    ? cleanText(trigger.textContent) || cleanText(flatText(trigger))
    : !(trigger instanceof HTMLInputElement) && trigger.getAttribute("role") === "combobox" && trigger.getAttribute("aria-expanded") !== "true"
      ? ownDisplayText(trigger)
      : "";
  const candidates = [
    trigger instanceof HTMLInputElement ? trigger.value : "",
    activeDescendantText(trigger),
    ...valueContainerTexts(trigger),
    ownText && !isPlaceholderFiller(ownText) ? ownText : "",
    isButtonLikeTrigger(trigger) ? textBesidePrompt(trigger) : "",
    siblingDisplayText(trigger),
  ];
  for (const c of candidates) {
    const v = cleanText(c);
    if (v && !DISPLAY_PROMPT.test(v)) return v;
  }
  return undefined;
}

/** What a dropdown shows before anything is chosen: "Select a state",
 *  "Choose your country…" (Paylocity, live 2026-10-05: read as an answer, the
 *  State was never selected for the fill). Only for a displayed value: as a
 *  label, "Select your preferred location" is a real question. */
const DISPLAY_PROMPT = /^[-\s]*(please\s+)?(select|choose|pick)\b[^.?!:]{0,30}(\.{3}|…)?$/i;

/**
 * The value a div combobox displays as its own text, without the option list
 * it keeps mounted inside it once opened: react-widgets' DropdownList on
 * Paylocity reads "--" + "--", "Yes", "No" whole, which "contains" Yes while
 * nothing is chosen. "" for a placeholder ("--", "Select…").
 */
function ownDisplayText(trigger: HTMLElement): string {
  const owned = new Set(
    [trigger.getAttribute("aria-owns"), trigger.getAttribute("aria-controls")].flatMap((ids) => (ids ? ids.split(/\s+/) : []))
  );
  const parts: string[] = [];
  const walk = (node: Node): void => {
    if (node.nodeType === Node.TEXT_NODE) {
      parts.push(node.textContent ?? "");
      return;
    }
    if (!(node instanceof Element)) return;
    if (node.matches('[role="listbox"], [role="option"], [aria-hidden="true"], [class*="popup" i]') || (node.id && owned.has(node.id))) return;
    node.childNodes.forEach(walk);
  };
  trigger.childNodes.forEach(walk);
  const text = cleanText(parts.join(" "));
  return !text || /^-+$/.test(text) || isPlaceholderFiller(text) ? "" : text;
}

/**
 * A button that keeps its prompt beside the pick, each in its own element:
 * Carvana's shows "Select answer" above the chosen "Yes" (`select__label`,
 * `select__inputValue`), so it reads "Select answerYes", a prompt by its first
 * word, and every pick there read back empty while the page held it (four
 * "reverted" questions, live 2026-10-17). Its text pieces that are no prompt;
 * "" when no prompt is among them (the whole text already stands) or nothing
 * else is.
 */
function textBesidePrompt(trigger: HTMLElement): string {
  const pieces: string[] = [];
  const walk = (node: Node): void => {
    if (node.nodeType === Node.TEXT_NODE) {
      const t = cleanText(node.textContent);
      if (t) pieces.push(t);
      return;
    }
    if (!(node instanceof Element) || node.matches('[role="listbox"], [role="option"], svg, [aria-hidden="true"]')) return;
    node.childNodes.forEach(walk);
  };
  trigger.childNodes.forEach(walk);
  const prompt = (p: string): boolean => isPlaceholderFiller(p) || DISPLAY_PROMPT.test(p);
  const rest = pieces.filter((p) => !prompt(p));
  return rest.length > 0 && rest.length < pieces.length ? cleanText(rest.join(" ")) : "";
}

/** A trigger whose own text is its displayed value rather than a caption. */
function isButtonLikeTrigger(trigger: HTMLElement): boolean {
  return (
    trigger.tagName === "BUTTON" ||
    (trigger.getAttribute("aria-haspopup") || "").toLowerCase() === "listbox"
  );
}

/**
 * Text of the option referenced by aria-activedescendant, if any, and only
 * while the list is CLOSED. In an open list the active descendant is the
 * keyboard HIGHLIGHT, not the selection: SpaceX's react-select was left open
 * with option 0 highlighted, option 0 was the answer, and the field counted as
 * already filled, so it was never driven and stayed empty (live 2026-10-03).
 */
function activeDescendantText(trigger: HTMLElement): string {
  if (trigger.getAttribute("aria-expanded") === "true") return "";
  const active = trigger.getAttribute("aria-activedescendant");
  if (!active) return "";
  const opt = byIdNear(trigger, active);
  return opt ? optionText(opt) : "";
}

const VALUE_DISPLAY_SELECTOR =
  '[class*="single-value" i], [class*="singlevalue" i], [class*="multi-value" i], [class*="multivalue" i], ' +
  // Chip/pill widgets (Workday's selectedItem, generic tag/token multiselects)
  // show committed values outside any *-value class, without these a
  // successful chip add verifies as "didn't stick".
  `[data-automation-id*="${WD_SELECTED_ITEM_FRAGMENT}" i], ` +
  '[class*="chip" i], [class*="pill" i], [class*="token" i], ' +
  // Ant Design's select (rc-select) shows the choice beside its search box,
  // whose value stays empty (Dayforce, live 2026-10-05: Country and State
  // read "didn't stick" while showing Canada and Ontario).
  '[class*="select-selection-item" i]';

/** Texts of react-select-style single/multi-value display elements near the trigger.
 *  The committed-value div is a COUSIN of the input (react-select: value-container
 *  > single-value + input-container > input), so `closest('[class*=select]')`,
 *  which stops at the innermost `select__input-container`: can never see it.
 *  Climb a few ancestors and query at each level until something shows. */
/**
 * A trigger button that shows nothing itself, beside the element showing the
 * choice: Duolingo's careers site renders `<span>Yes</span><button
 * aria-haspopup="listbox">` (live 2026-10-05). Read through the button alone,
 * every pick there was reported "didn't stick", 13 of them on one page. The
 * text of the button's own wrapper, without the button, a list or an icon;
 * "" for a placeholder.
 */
function siblingDisplayText(trigger: HTMLElement): string {
  if (trigger.tagName !== "BUTTON" || cleanText(trigger.textContent) || trigger.getAttribute("aria-haspopup") !== "listbox") return "";
  const wrap = trigger.parentElement;
  if (!wrap || wrap.querySelectorAll('[aria-haspopup="listbox"], [role="combobox"]').length > 1) return "";
  const parts: string[] = [];
  for (const child of Array.from(wrap.children)) {
    if (child === trigger || child.matches('[role="listbox"], [role="option"], svg, button, input, [aria-hidden="true"]')) continue;
    parts.push(cleanText(child.textContent));
  }
  const text = cleanText(parts.join(" "));
  return text && !isPlaceholderFiller(text) && !/^[-–—.…\s]+$/.test(text) ? text : "";
}

function valueContainerTexts(trigger: HTMLElement): string[] {
  let node: HTMLElement | null = trigger.parentElement;
  for (let hops = 0; node && node !== node.ownerDocument.body && hops < 6; hops++, node = node.parentElement) {
    // Climbed past our widget into a container holding other dropdowns, any
    // value found from here would be a NEIGHBOR field's selection, not ours.
    if (node.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"]').length > 1) break;
    const texts = Array.from(node.querySelectorAll(VALUE_DISPLAY_SELECTOR))
      .map((e) => cleanText(e.textContent))
      .filter(Boolean);
    if (texts.length > 0) return texts;
  }
  return [];
}

// ---------------------------------------------------------------------------
// Verification
// ---------------------------------------------------------------------------

/** Whether the combobox's committed/displayed value reflects the target.
 *  `selfTyped` is the filter text WE typed into the input this run, when the
 *  input still holds exactly that, it is our own echo, not commit evidence. */
function comboboxShowsValue(trigger: HTMLElement, value: string, selfTyped?: string | null): boolean {
  const candidates: string[] = [];
  if (trigger instanceof HTMLInputElement && trigger.value && trigger.value !== selfTyped) {
    candidates.push(trigger.value);
  }
  // A web-component button shows its selection through a <slot>, outside
  // its own textContent (SmartRecruiters' phone "Country code").
  if (trigger.tagName === "BUTTON") candidates.push(cleanText(trigger.textContent) || cleanText(flatText(trigger)));
  // A div combobox that IS the display: react-widgets' DropdownList shows the
  // choice as its own text (Paylocity's "Did you Graduate?" read "didn't
  // stick" while it showed "Yes", live 2026-10-03). Its open list is inside
  // it, so only a closed widget's text counts.
  else if (!(trigger instanceof HTMLInputElement) && trigger.getAttribute("aria-expanded") !== "true") {
    const own = ownDisplayText(trigger);
    if (own) candidates.push(own);
  }
  // SAP SuccessFactors' rcmpaginatedselect commits the choice into the input's
  // `title` while leaving `value` empty and the placeholder ("No Selection")
  // in place, so a successful selection read as "didn't stick" without this.
  const title = cleanText(trigger.getAttribute("title"));
  if (title && !/^no selection$/i.test(title)) candidates.push(title);
  const active = activeDescendantText(trigger);
  if (active) candidates.push(active);
  candidates.push(...valueContainerTexts(trigger));
  const beside = siblingDisplayText(trigger);
  if (beside) candidates.push(beside);
  return candidates.some((c) => textMatches(c, value));
}

function textMatches(displayed: string, target: string): boolean {
  const a = normalize(displayed);
  const b = normalize(target);
  if (!a || !b) return false;
  return a === b || a.includes(b) || b.includes(a);
}

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

function isVisible(el: HTMLElement): boolean {
  if (el.hasAttribute("hidden")) return false;
  const style = el.ownerDocument.defaultView?.getComputedStyle(el);
  if (style && (style.display === "none" || style.visibility === "hidden")) return false;
  // Computed style is per-element and does NOT reflect a display:none ANCESTOR,
  // intl-tel-input keeps its 244-option dial-code listbox mounted inside a hidden
  // dialog, and grabbing it cross-contaminates every dropdown on the page. A
  // rendered listbox always has client rects; a hidden-subtree one has none.
  return el.getClientRects().length > 0;
}

async function waitFor<T>(
  probe: () => T | null,
  sleep: (ms: number) => Promise<void>,
  budgetMs: number,
  pollMs: number
): Promise<T | null> {
  let elapsed = 0;
  for (;;) {
    const hit = probe();
    if (hit) return hit;
    if (elapsed >= budgetMs) return null;
    await sleep(pollMs);
    elapsed += pollMs;
  }
}

function truncate(s: string, max = 40): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}
