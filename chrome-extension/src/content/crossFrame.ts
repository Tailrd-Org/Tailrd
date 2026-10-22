/**
 * Pure cross-frame helpers: no chrome.* and no DOM, so they unit-test cleanly.
 * The chrome-messaging plumbing that uses these lives in contentScript.ts.
 *
 * When a job-application form lives in a cross-origin iframe (e.g. Databricks'
 * embedded Greenhouse form), the panel stays in the top frame and every overlay
 * operation is marshaled to the owning frame as a generic {op, args} message.
 */
import type { FormOpName, FormOpResult } from "../shared/types";
import type { OverlayCallbacks } from "./overlay";

/** Void callbacks: the proxy fires them and does not wait for a value. */
const VOID_OPS: ReadonlySet<FormOpName> = new Set<FormOpName>([
  "onRescan",
  "onProfileResolved",
  "onFlowStop",
  "onFlowAdvance",
  "onFlowPause",
]);

/** All OverlayCallbacks method names, in one place for the proxy factory. */
const ALL_OPS: FormOpName[] = [
  "onAutofill",
  "onRescan",
  "onListResumes",
  "onUploadResume",
  "onTailorResume",
  "onAttachTailored",
  "onDownloadTailored",
  "onGenerateCoverLetter",
  "onInsertCoverLetter",
  "onDownloadCoverLetter",
  "onCopyCoverLetter",
  "onProfileResolved",
  "onFlowStop",
  "onFlowAdvance",
  // The countdown's Pause: the flow runs in the form's frame, the button is
  // in the panel's, so without it here Pause would do nothing on an embedded form.
  "onFlowPause",
  // The unanswered-questions modal. onAnswerGaps was missing here, so in a
  // cross-origin form frame `callbacks.onAnswerGaps` was undefined and Save &
  // fill threw before it wrote anything, the modal simply did not work off the
  // top frame. Both return a value, so neither belongs in VOID_OPS.
  "onAnswerGaps",
  "onHarvestGapOptions",
];

/** The top frame defers to a child host only when it has no form of its own. */
export function shouldAdoptRemoteHost(localRecognized: number, remoteRecognized: number): boolean {
  return localRecognized === 0 && remoteRecognized > 0;
}

/**
 * Whether the top frame adopts a child frame's form now: one frame only, and
 * only beside no form of its own. A panel already shown for the posting's own
 * page adopts too: Comeet's job page mounted it for "Apply for this job", the
 * form then opened in comeet.co's frame, and the Autofill found "No
 * application form found on this page" (live, round 8).
 */
export function shouldAdoptFrame(state: { adopted: boolean; panelShown: boolean }, localRecognized: number, remoteRecognized: number): boolean {
  return !state.adopted && shouldAdoptRemoteHost(localRecognized, remoteRecognized);
}

/**
 * An OverlayCallbacks whose every method marshals to `send(op, args)` and
 * returns the unwrapped value. Void methods (rescan / profile-resolved) fire
 * and forget, the host pushes fields back over the update channel.
 */
export function makeProxyCallbacks(
  send: (op: FormOpName, args: unknown[]) => Promise<FormOpResult>
): OverlayCallbacks {
  const call = (op: FormOpName, args: unknown[]): Promise<unknown> =>
    send(op, args).then((r) => {
      if (!r.ok) throw new Error(r.error ?? `Form op ${op} failed`);
      return r.value;
    });

  const proxy = {} as Record<FormOpName, (...args: unknown[]) => unknown>;
  for (const op of ALL_OPS) {
    proxy[op] = VOID_OPS.has(op)
      ? (...args: unknown[]): void => {
          void call(op, args).catch(() => {});
        }
      : (...args: unknown[]): Promise<unknown> => call(op, args);
  }
  return proxy as unknown as OverlayCallbacks;
}

/** Run one overlay op against the local callbacks, wrapping the outcome. */
export async function dispatchFormOp(
  ops: OverlayCallbacks,
  op: FormOpName,
  args: unknown[]
): Promise<FormOpResult> {
  try {
    const fn = ops[op] as (...a: unknown[]) => unknown;
    const value = await fn(...args);
    return { ok: true, value };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}
