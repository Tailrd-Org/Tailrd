/**
 * MAIN-world autofill driver. Runs in the PAGE's JS world (injected by the
 * service worker), so it can read React's Fiber (`__reactFiber$…`, invisible to
 * the isolated content script) and drive react-select / Workday widgets through
 * their real React callbacks. Communicates with the isolated client purely over
 * CustomEvents with JSON detail. No chrome.* here.
 */
import { FIELD_ID_ATTR } from "../shared/constants";
import {
  MW_FILL_EVENT,
  MW_RESULT_EVENT,
  type MwFillDetail,
  type MwResultDetail,
} from "./mainWorldBridge";
import {
  PROMPT_BUTTON_SELECTOR as WD_PROMPT_BUTTON_SELECTOR,
  PROMPT_OPTION_SELECTOR as WD_PROMPT_OPTION_SELECTOR,
} from "./adapters/workdaySelectors";
import { matchOption } from "./optionMatch";
import { looksLikePlaces, pickPlaceOption } from "./placeMatch";
import { snapSchool } from "./schoolMatch";

const norm = (s: string): string => s.toLowerCase().replace(/\s+/g, " ").trim();

/**
 * Index of the option label `target` selects, or −1: the SAME strict matcher
 * the content script uses (optionMatch.ts). This used to be a looser copy:
 * substring containment where the first containing option won, so "2027"
 * selected "December 2026 - November 2027" although "December 2027 - November
 * 2028" fits too (Greenhouse, live 2026-10-03), and "male" sits inside
 * "female".
 */
export function pickOption(labels: string[], target: string, placeHint?: string, school?: boolean): number {
  // Place suggestions are chosen as PLACES, from the applicant's full place:
  // "San Jose" picked "San José, Costa Rica" for a Californian (Zipline's
  // embedded Greenhouse form, live 2026-10-03).
  if (placeHint && looksLikePlaces(labels)) return pickPlaceOption(labels, placeHint);
  // A school by its own words: "Austin Peay State University" shares two with
  // "The University of Texas at Austin" (Everpure, live 2026-10-17).
  if (school) {
    const hit = snapSchool(labels, target);
    return hit === null ? -1 : labels.indexOf(hit);
  }
  const hit = matchOption(
    labels.map((_, i) => i),
    (i) => labels[i],
    (i) => labels[i],
    target
  );
  return hit ?? -1;
}

/** React attaches its Fiber under a per-render key on each host node. */
export function getFiber(el: Element): FiberNode | null {
  const key = Object.keys(el).find(
    (k) => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")
  );
  return key ? ((el as unknown as Record<string, FiberNode>)[key] ?? null) : null;
}

/** Walk up the fiber return chain, returning the first node matching `pred`. */
export function climbFiber(start: FiberNode | null, pred: (f: FiberNode) => boolean): FiberNode | null {
  let f = start;
  for (let i = 0; i < 60 && f; i++, f = f.return) if (pred(f)) return f;
  return null;
}

export interface FiberNode {
  return: FiberNode | null;
  stateNode: unknown;
  memoizedProps?: Record<string, unknown> | null;
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/** The option labels the last missed fill saw (fillField reports them). */
let missOptions: string[] = [];

/**
 * `CSS.escape` per spec, with a same-behavior fallback for environments
 * where the `CSS` global is unavailable (this project's jsdom test
 * environment does not implement it; every Chrome version this extension
 * targets, 110+, has full native support). Field ids are always plain
 * tokens assigned by formScanner.ts, so the fallback only needs to keep the
 * attribute selector well-formed, not be spec-perfect.
 */
const escapeAttrValue = (s: string): string =>
  typeof CSS !== "undefined" && typeof CSS.escape === "function"
    ? CSS.escape(s)
    : s.replace(/["\\]/g, "\\$&");

async function fillField(doc: Document, detail: MwFillDetail): Promise<MwResultDetail> {
  // The whole body is wrapped so this NEVER rejects, installDriver's bridge
  // reply must always fire, or the isolated-world caller hangs forever.
  try {
    const el = doc.querySelector<HTMLElement>(`[${FIELD_ID_ATTR}="${escapeAttrValue(detail.fieldId)}"]`);
    if (!el) return { id: detail.id, ok: false, reason: "field-not-found" };
    missOptions = [];
    const committed =
      detail.kind === "react-select"
        ? await fillReactSelect(el, detail.value, detail.placeHint, detail.school)
        : await fillWorkday(el, detail.value);
    return committed === null
      ? { id: detail.id, ok: false, reason: "no-match", ...(missOptions.length ? { options: missOptions.slice(0, 300) } : {}) }
      : { id: detail.id, ok: true, committed };
  } catch (err) {
    return { id: detail.id, ok: false, reason: err instanceof Error ? err.message : "driver-error" };
  }
}

interface RsInstance {
  props: {
    options?: unknown[];
    getOptionLabel?: (o: unknown) => string;
    onChange?: (o: unknown, meta: { action: string }) => void;
  };
  selectOption?: (o: unknown) => void;
}

function rsLabel(inst: RsInstance, opt: unknown): string {
  if (inst.props.getOptionLabel) return inst.props.getOptionLabel(opt);
  const o = opt as { label?: string; value?: string };
  return o.label ?? o.value ?? String(opt);
}

/** Flatten react-select options (supports grouped `{ options: [...] }`). */
function rsFlatten(options: unknown[]): unknown[] {
  const out: unknown[] = [];
  for (const o of options) {
    const grp = o as { options?: unknown[] };
    if (grp && Array.isArray(grp.options)) out.push(...grp.options);
    else out.push(o);
  }
  return out;
}

/** The widget's editable input (works for default and custom inputIds). */
function rsInput(scope: HTMLElement): HTMLInputElement | null {
  return scope.querySelector<HTMLInputElement>('input[id^="react-select"], input[role="combobox"]');
}

/** Outermost wrapper of the react-select widget around `el` (≤6 hops). */
function rsWidgetHost(el: HTMLElement): HTMLElement {
  let host = el;
  let node = el.parentElement;
  for (let hops = 0; node && hops < 6; hops++, node = node.parentElement) {
    if (!/select|combobox|container|control|dropdown/i.test(node.getAttribute("class") ?? "")) break;
    host = node;
  }
  return host;
}

async function fillReactSelect(el: HTMLElement, value: string, placeHint?: string, school?: boolean): Promise<string | null> {
  const container = rsWidgetHost(el);
  const inst = climbFiber(getFiber(el.closest('[class*="-container"], [class*="__container"]') ?? container), (f) => {
    const sn = f.stateNode as RsInstance | null;
    return Boolean(sn && (typeof sn.selectOption === "function" || sn.props?.onChange) && Array.isArray(sn.props?.options));
  })?.stateNode as RsInstance | undefined;

  if (inst && inst.props.options) {
    const labels = (): string[] => rsFlatten(inst.props.options ?? []).map((o) => rsLabel(inst, o)).filter((l): l is string => typeof l === "string" && l.trim() !== "");
    const pick = (): { opt: unknown; label: string } | null => {
      const flat = rsFlatten(inst.props.options ?? []);
      const idx = pickOption(flat.map((o) => rsLabel(inst, o)), value, placeHint, school);
      return idx >= 0 ? { opt: flat[idx], label: rsLabel(inst, flat[idx]) } : null;
    };
    missOptions = labels();
    let hit = pick();
    // No match yet: async selects (Places city lookup…) only load options after
    // the user types. Type the value through the real input and poll the
    // instance's props.options (they refresh on every load) before giving up.
    if (!hit) {
      const input = rsInput(container);
      if (input) {
        input.focus();
        setNativeInputValue(input, value);
        for (let waited = 0; !hit && waited < 3500; waited += 150) {
          await sleep(150);
          hit = pick();
        }
        if (!hit) {
          // What the search offered, or the whole list it filters.
          const searched = labels();
          if (searched.length > 0) missOptions = searched;
          // Clear our filter text before closing, leaving it in the box reads
          // as an entered answer sitting on top of (or instead of) the value.
          setNativeInputValue(input, "");
          fireKey(input, "Escape");
        }
      }
    }
    if (!hit) return null;
    if (typeof inst.selectOption === "function") inst.selectOption(hit.opt);
    else inst.props.onChange?.(hit.opt, { action: "select-option" });
    return hit.label;
  }
  return fillReactSelectByDom(container, value, placeHint, school);
}

/** Fallback when no instance is found: open, filter, click the option in page context. */
async function fillReactSelectByDom(container: HTMLElement, value: string, placeHint?: string, school?: boolean): Promise<string | null> {
  const input = rsInput(container);
  const opener = (input as HTMLElement) ?? container;
  fireMouse(opener, "mousedown");
  opener.focus?.();
  if (input) setNativeInputValue(input, value);

  // Menu + options mount lazily (and async selects fetch them), poll instead of
  // a single fixed hop. Menus portal to <body>, so fall back to a document-wide
  // query, but never accept a menu that belongs to a different widget.
  let options: HTMLElement[] = [];
  for (let waited = 0; options.length === 0 && waited < 2400; waited += 120) {
    await sleep(120);
    const menus = [
      ...Array.from(container.querySelectorAll<HTMLElement>('[class*="-menu"], [class*="__menu"], [role="listbox"]')),
      ...Array.from(document.querySelectorAll<HTMLElement>('[class*="-menu"], [class*="__menu"], [role="listbox"]')).filter(
        (m) => !m.closest('[class*="-container"], [class*="__container"]') || m.closest('[class*="-container"], [class*="__container"]')!.contains(opener)
      ),
    ];
    for (const menu of menus) {
      options = Array.from(menu.querySelectorAll<HTMLElement>('[class*="-option"], [class*="__option"], [role="option"]'));
      if (options.length > 0) break;
    }
  }
  const idx = pickOption(options.map((o) => norm(o.textContent ?? "")), value, placeHint, school);
  if (idx < 0) {
    missOptions = options.map((o) => norm(o.textContent ?? "")).filter(Boolean);
    fireKey(opener, "Escape");
    return null;
  }
  fireMouse(options[idx], "mousedown");
  fireMouse(options[idx], "mouseup");
  options[idx].click();
  await sleep(60);
  const single = container.querySelector('[class*="-singleValue"], [class*="__single-value"], [class*="-single-value"], [class*="single-value"]');
  return norm(single?.textContent ?? options[idx].textContent ?? "") || null;
}

/**
 * `view: window` matches real dispatched-event semantics (every Chrome
 * version this extension targets has `window instanceof Window` true).
 * Vitest's jsdom test environment aliases the global `window` to
 * `globalThis` for realistic global-scope emulation, so it fails jsdom's
 * `instanceof Window` brand check there and the constructor throws, guard
 * with the same feature-detect fallback style as `escapeAttrValue` above,
 * rather than drop `view` outright.
 */
function fireMouse(el: HTMLElement, type: string): void {
  const view = typeof Window !== "undefined" && window instanceof Window ? window : undefined;
  el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view }));
}
function fireKey(el: HTMLElement, key: string): void {
  el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
}
function setNativeInputValue(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter ? setter.call(input, value) : (input.value = value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

interface WorkdayProps {
  options?: unknown[];
  onChange?: (o: unknown) => void;
  onValueChange?: (o: unknown) => void;
}

/** Prefer the widget's React onChange (via memoizedProps on the fiber), else drive
 *  Workday's `data-automation-id` prompt list in page context. */
async function fillWorkday(el: HTMLElement, value: string): Promise<string | null> {
  const widget = el.closest("[data-automation-id]") ?? el;

  const propFiber = climbFiber(getFiber(widget), (f) => {
    const p = f.memoizedProps as WorkdayProps | null;
    return Boolean(p && Array.isArray(p.options) && (p.onChange || p.onValueChange));
  });
  const props = propFiber?.memoizedProps as WorkdayProps | undefined;
  if (props?.options) {
    const labels = props.options.map((o) => {
      const x = o as { label?: string; value?: string };
      return x.label ?? x.value ?? String(o);
    });
    const idx = pickOption(labels, value);
    if (idx < 0) return null;
    (props.onChange ?? props.onValueChange)?.(props.options[idx]);
    return labels[idx];
  }

  // DOM fallback: open the prompt, wait for options, click the match.
  const opener =
    widget.querySelector<HTMLElement>(`${WD_PROMPT_BUTTON_SELECTOR}, button`) ?? (widget as HTMLElement);
  fireMouse(opener, "mousedown");
  fireMouse(opener, "click");
  await sleep(80);
  const options = Array.from(
    (widget.ownerDocument ?? document).querySelectorAll<HTMLElement>(
      `${WD_PROMPT_OPTION_SELECTOR}, [role="option"]`
    )
  );
  const idx = pickOption(options.map((o) => norm(o.textContent ?? "")), value);
  if (idx < 0) return null;
  fireMouse(options[idx], "mousedown");
  fireMouse(options[idx], "mouseup");
  options[idx].click();
  await sleep(30);
  return norm(opener.textContent ?? options[idx].textContent ?? "") || null;
}

/** Install the fill listener once per frame. Idempotent via a window guard.
 *  The handler ref is stored so the test seam (__test.uninstall) can tear it
 *  down between tests; in production installDriver runs exactly once per frame. */
export function installDriver(win: Window & typeof globalThis): void {
  const w = win as unknown as Record<string, unknown>;
  if (w.__tailrdMWInstalled) return;
  w.__tailrdMWInstalled = true;
  const handler = (e: Event): void => {
    const detail = (e as CustomEvent<MwFillDetail>).detail;
    if (!detail || typeof detail.id !== "number") return;
    void fillField(win.document, detail).then((result) => {
      win.dispatchEvent(new CustomEvent(MW_RESULT_EVENT, { detail: result }));
    });
  };
  w.__tailrdMWHandler = handler;
  win.addEventListener(MW_FILL_EVENT, handler);
}

// Exported for tests only.
export const __test = {
  fillField,
  sleep,
  /** Test-only teardown: remove the installed listener and clear the guard so a
   *  fresh installDriver in the next test doesn't accumulate listeners. */
  uninstall(win: Window & typeof globalThis): void {
    const w = win as unknown as Record<string, unknown>;
    const handler = w.__tailrdMWHandler as EventListener | undefined;
    if (handler) win.removeEventListener(MW_FILL_EVENT, handler);
    delete w.__tailrdMWHandler;
    delete w.__tailrdMWInstalled;
  },
};
