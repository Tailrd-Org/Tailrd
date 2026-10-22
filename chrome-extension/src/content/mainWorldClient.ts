/**
 * Isolated-world side of the MAIN-world driver bridge. Ensures the driver is
 * injected into this frame (asking the service worker, which owns chrome.scripting),
 * then dispatches a fill request and awaits the matching result with a timeout.
 */
import {
  MW_FILL_EVENT,
  MW_RESULT_EVENT,
  MW_SUPPRESS_EVENT,
  type FillDriver,
  type MwFillDetail,
  type MwResultDetail,
  type MwSuppressDetail,
} from "./mainWorldBridge";

export interface DriverResult { ok: boolean; committed?: string; reason?: string; options?: string[]; }

// Must exceed the MAIN-world driver's worst case: async selects (Places city
// lookup) poll options for up to ~3.5s after typing before reporting no-match.
const DEFAULT_TIMEOUT_MS = 6000;
let installed: Promise<boolean> | null = null;
let nextId = 1;

/** Test-only: forget the memoized install so each test starts clean. */
export function __resetDriverInstall(): void { installed = null; }

function ensureInstalled(): Promise<boolean> {
  if (!installed) {
    installed = chrome.runtime
      .sendMessage({ type: "INSTALL_MAIN_WORLD_DRIVER" })
      .then((r: { ok?: boolean } | undefined) => Boolean(r?.ok))
      .catch(() => false);
  }
  return installed;
}

/**
 * Toggle MAIN-world blocking-dialog suppression for the duration of an active
 * fill/flow. Turning it on injects the driver first (best-effort, if injection
 * fails we simply don't suppress); turning it off just dispatches the toggle.
 */
export async function setDialogSuppression(on: boolean): Promise<void> {
  if (on && !(await ensureInstalled())) return;
  const detail: MwSuppressDetail = { on };
  window.dispatchEvent(new CustomEvent(MW_SUPPRESS_EVENT, { detail }));
}

export async function driveField(
  fieldId: string,
  value: string,
  kind: FillDriver,
  opts: { timeoutMs?: number; placeHint?: string; school?: boolean; demographic?: string } = {}
): Promise<DriverResult> {
  // If the MAIN-world driver couldn't be injected into this frame, fail fast to
  // needs-manual instead of dispatching into the void and waiting out the full
  // timeout for every driver field (which would feel like a hang).
  if (!(await ensureInstalled())) {
    return { ok: false, reason: "driver-uninstalled" };
  }
  const id = nextId++;
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  return new Promise<DriverResult>((resolve) => {
    let done = false;
    const finish = (r: DriverResult): void => {
      if (done) return;
      done = true;
      window.removeEventListener(MW_RESULT_EVENT, onResult);
      clearTimeout(timer);
      resolve(r);
    };
    // Trust boundary: a hostile page could dispatch a forged tailrd:mw:result with
    // a guessed id. Acceptable. It can only affect the accuracy of a fill into the
    // page's OWN form; no token or cross-origin data is exposed to page context.
    const onResult = (e: Event): void => {
      const d = (e as CustomEvent<MwResultDetail>).detail;
      if (!d || d.id !== id) return;
      finish({ ok: d.ok, committed: d.committed, reason: d.reason, options: d.options });
    };
    const timer = setTimeout(() => finish({ ok: false, reason: "driver-timeout" }), timeoutMs);
    window.addEventListener(MW_RESULT_EVENT, onResult);
    const detail: MwFillDetail = { id, fieldId, value, kind, ...(opts.placeHint ? { placeHint: opts.placeHint } : {}), ...(opts.school ? { school: true } : {}), ...(opts.demographic ? { demographic: opts.demographic } : {}) };
    window.dispatchEvent(new CustomEvent(MW_FILL_EVENT, { detail }));
  });
}
