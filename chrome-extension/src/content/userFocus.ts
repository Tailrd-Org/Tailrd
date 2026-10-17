/**
 * Whose is the focus? The reconciler never writes into a box the user is in
 * (a write focuses and blurs it, yanking the caret). But a box the PAGE
 * focused is not one the user is typing in: iCIMS's login email has
 * `autofocus`, so on a full-page login it held focus from load, the guard
 * took it for the user's, and the email was never written (Kinaxis, live
 * 2026-10-17).
 *
 * Per document: the focus is the user's when it landed right after the user
 * pressed a pointer or a key in the page (the extension's own panel aside),
 * or when the user typed in the box. Focus given with no such action, an
 * autofocus or the page's own script, is the page's.
 */
import { EXTENSION_UI_HOST_IDS } from "./domUtils";

/** How soon after a click or key a focus still counts as its result. */
const USER_FOCUS_MS = 1500;

interface Tracker {
  lastAction: number;
  /** Where the focus was when it last became the user's. */
  userFocus: EventTarget | null;
}

const trackers = new WeakMap<Document, Tracker>();

function inOwnUi(e: Event): boolean {
  return e.composedPath().some((n) => EXTENSION_UI_HOST_IDS.has((n as Element).id ?? ""));
}

/** Start telling the user's focus from the page's in `doc`. Idempotent. */
export function trackUserFocus(doc: Document): void {
  if (trackers.has(doc)) return;
  const t: Tracker = { lastAction: Number.NEGATIVE_INFINITY, userFocus: null };
  trackers.set(doc, t);
  const now = (): number => (doc.defaultView?.performance ?? performance).now();
  const onAction = (e: Event): void => {
    if (inOwnUi(e)) return;
    t.lastAction = now();
    // A key pressed while a box has focus: the user is in that box.
    if (e.type === "keydown" && doc.activeElement && doc.activeElement !== doc.body) t.userFocus = doc.activeElement;
  };
  const onFocusIn = (e: Event): void => {
    t.userFocus = now() - t.lastAction <= USER_FOCUS_MS ? e.target : null;
  };
  for (const type of ["pointerdown", "mousedown", "touchstart", "keydown"]) doc.addEventListener(type, onAction, true);
  doc.addEventListener("focusin", onFocusIn, true);
}

/** Forget what was seen in `doc` (tests share one document between cases). */
export function forgetUserFocus(doc: Document): void {
  const t = trackers.get(doc);
  if (t) {
    t.lastAction = Number.NEGATIVE_INFINITY;
    t.userFocus = null;
  }
}

/**
 * True when the focused element of `doc` is where the user put the focus.
 * Untracked documents keep the old, cautious answer: every focus is the user's.
 */
export function focusIsUsers(doc: Document): boolean {
  const t = trackers.get(doc);
  if (!t) return true;
  return t.userFocus !== null && t.userFocus === doc.activeElement;
}
