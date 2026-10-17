/**
 * Content script entry point.
 *
 * Runs in every frame of supported ATS pages (declared in the manifest) and
 * is injected on demand into any other page via the popup's Scan button.
 *
 * Two ways it is used:
 *  1. Autonomously, on the top frame it scans for an application form and,
 *     when fields are found, mounts the in-page overlay (FAB + full popup UI).
 *  2. On demand, the toolbar popup sends SCAN_PAGE / FILL_FIELDS messages.
 *
 * Frame coordination: chrome.tabs.sendMessage broadcasts to all frames but
 * resolves with the FIRST response. We exploit that deliberately:
 *  - SCAN: frames that found fields answer immediately; empty frames answer
 *    after a delay, so a form living inside an iframe (embedded Greenhouse)
 *    wins the race over an empty top frame.
 *  - FILL: field ids are prefixed with a per-frame token, so only the frame
 *    that owns the fields responds.
 */
import type {
  AiFillResponse,
  ApplicationLog,
  BackgroundRequest,
  ContentRequest,
  CoverLetterGenOpts,
  DetectedField,
  DroppedAnswer,
  FieldsUpdatedEvent,
  FillResponse,
  FlowProgress,
  FlowState,
  FlowStateResponse,
  JobPlaceResponse,
  FormOpName,
  FormOpResult,
  JobContext,
  OverridesResponse,
  GenerateCoverLetterResponse,
  PingResponse,
  RecordApplicationResponse,
  AccessTokenResponse,
  RenderCoverLetterResponse,
  RenderResumeResponse,
  ResumeDoc,
  ResumeFileResponse,
  ResumeSummary,
  ResumesResponse,
  ScanResponse,
  ProfileResponse,
  SimpleResponse,
  TailorResumeOpts,
  TailorResumeResponse,
  UserApplicationProfile,
} from "../shared/types";
import { deepQueryAll, isVisible, cleanText } from "./domUtils";
import { controlVisibility, formLooksHidden, waitForFormReveal } from "./hiddenForm";
import { base64ToFile, downloadBase64File, injectResumeFile, type UploadResult } from "./fileUpload";
import { openAiModal } from "./aiModalBridge";
import { getConfig, getLastJobContext, saveLastJobContext } from "../shared/storage";

const MIN_CACHEABLE_DESC = 200;
import { FRAME_TOKEN, observePage, resolveWithOptions, scanPage, selectOptions, type RuntimeControl } from "./formScanner";
import { LONG_TEXT, normalize } from "./fieldMatcher";
import { answersWorthRemembering, planAnswerSaves } from "./answerGaps";
import { customFieldAnswers, getExtras } from "./autofillExtras";
import { AutofillReconciler, type FieldReport } from "./reconciler";
import { liveControlFor } from "./staleControl";
import { loadWorkableGuesses, workableShortcode } from "./pageGuesses";
import { defaultSelectedIds, fillSelection, isDefaultSelected } from "../shared/selection";
import { extractJobContext, extractJobIdentity } from "./jobContext";
import { detectJobPlace, sanitizeCompany, type JobPlace } from "./jobLocation";
import { getResolveContext, setResolveContext } from "./fieldResolver";
import { formCountryHint } from "./questionResolver";
import { isHigh, profileFacts } from "./profileFacts";
import { aiFillCandidates, isBoolish, needsOptionHarvest, planAiFill, planFillRoute, planOnDeviceReask, planReaskFields, planSensitiveReask, tallyOutcomes, toAiFillField, type PlannedAnswer, type ReaskCandidate } from "./aiFillPlanner";
import { toApplicantProfile } from "./applicantProfile";
import { splitByCache, cacheAnswers } from "./answerCache";
import { AUTOFILL_CONFIDENCE_THRESHOLD } from "../shared/constants";
import { activateElement, comboboxDisplaysValue, fillAriaCombobox, harvestComboboxOptions, readComboboxValue } from "./comboboxEngine";
import { SECTION_KINDS, MAX_ROWS, rowsPresent, rowsNeeded, findAddButton } from "./repeatingSections";
import { driveField, setDialogSuppression } from "./mainWorldClient";
import { dispatchFormOp, makeProxyCallbacks, shouldAdoptRemoteHost } from "./crossFrame";
import { matchOption, verifyControl, writeControl } from "./writeEngine";
import {
  showOverlay,
  updateOverlay,
  toggleOverlay,
  updateFlowProgress,
  showReloadRequired,
  type OverlayCallbacks,
} from "./overlay";
import { runAdapterOperations, type SiteAdapter } from "./adapters";
import { ERROR_FRAGMENT as WD_ERROR_FRAGMENT } from "./adapters/workdaySelectors";
import { detectSite } from "./siteRegistry";
import { FlowController, FLOW_TTL_MS, type FlowDeps, type FlowSnapshot, type StepTally } from "./flowController";
import { clickAdvance, findAdvanceButton, findPendingAdvance } from "./advance";
import { loneFormScope, type ScopeEntry } from "./formScope";
import { trackUserFocus } from "./userFocus";
import { findApplyEntry } from "./applyEntry";
import {
  hasUnsolvedCaptcha,
  isVerificationWall,
  resumeFieldForAttach,
  resumeFieldNeedingFile,
  validationMessages,
  holdsNoAnswer,
} from "./flowChecks";
import { detectWall, findSignupToggle, runAccountWall } from "./accountFlow";
import { getCredential } from "./credentialStore";
import { bindSubmitTracking, type SubmitTrackerHandle } from "./submitTracker";
import {
  buildAutofillTelemetry,
  finalOutcomes,
  revertedFields,
  type DroppedAnswerLike,
  type FieldProvenance,
  type ObservedField,
} from "./telemetry";
import { setOverrideRules } from "./overrides";
import { captureFieldDom, captureOptions, fieldSelector } from "./domCapture";
import { looksLikeJobApplication, type PageContext } from "./jobFormEvidence";
import { onExtensionContextInvalidated, postToRuntime, sendToRuntime } from "./runtimeMessaging";

// Guard against double injection (manifest match + programmatic inject).
declare global {
  interface Window {
    __apContentScriptLoaded?: boolean;
  }
}

/** This build's version, so a stale local extension is visible in telemetry
 *  rather than being investigated as a code bug (it has been, more than once). */
function extensionVersion(): string {
  try {
    return chrome.runtime.getManifest().version ?? "";
  } catch {
    return "";
  }
}

/**
 * Whether this account opted into diagnostic capture (answers + form markup).
 *
 * Resolved once per page: the service worker caches the server's answer, and
 * a failure resolves `false`, because the safe direction is always to capture
 * less rather than more.
 */
let diagnosticModeCheck: Promise<boolean> | null = null;
function isDiagnosticMode(): Promise<boolean> {
  diagnosticModeCheck ??= sendToBackground<{ ok?: boolean; enabled?: boolean }>({
    type: "DIAGNOSTIC_MODE",
  })
    .then((r) => Boolean(r?.enabled))
    .catch(() => false);
  return diagnosticModeCheck;
}

/**
 * Attach a downloaded résumé/cover file to `el`. When the site's upload can't be
 * scripted at all (a custom button that opens a native picker with no persistent
 * <input type=file>, SAP SuccessFactors), download the file so the user can pick
 * it in the site's own dialog, and say so.
 */
async function attachOrGuide(el: HTMLElement, dataBase64: string, name: string, contentType: string): Promise<UploadResult> {
  const result = await injectResumeFile(el, base64ToFile(dataBase64, name, contentType));
  if (!result.manual) return result;
  downloadBase64File(dataBase64, name, contentType);
  return {
    ok: false,
    reason: `This site opens its own file picker. I downloaded "${name}" to your Downloads. Click the page's upload button and choose it.`,
  };
}

if (!window.__apContentScriptLoaded) {
  window.__apContentScriptLoaded = true;
  initialize();
}

function sendToBackground<T>(message: BackgroundRequest): Promise<T> {
  // Routed through the context-safe sender: after an extension reload this
  // content script is orphaned and a raw sendMessage throws synchronously.
  return sendToRuntime<T>(message) as Promise<T>;
}

// --- TEMP diagnostics (remove before shipping) ------------------------------
// Logs, per frame, what the scanner actually sees so we can tell whether the
// form is missed entirely, partially detected, or living in a cross-origin
// iframe the panel can't reach. Deduped so dynamic pages don't spam.
let lastScanSig = "";
function logScanDiagnostics(
  isTopFrame: boolean,
  fields: DetectedField[],
  profileLoaded: boolean
): void {
  try {
    const rawControls = deepQueryAll(document, "input, textarea, select").length;
    const iframes = Array.from(document.querySelectorAll("iframe"));
    let crossOrigin = 0;
    for (const f of iframes) {
      try {
        if (!f.contentDocument) crossOrigin++;
      } catch {
        crossOrigin++;
      }
    }
    const withValue = fields.filter((f) => f.proposedValue !== null).length;
    const wouldAutoSelect = defaultSelectedIds(fields).size;
    const sig = `${rawControls}|${fields.length}|${withValue}|${wouldAutoSelect}|${profileLoaded}|${crossOrigin}`;
    if (sig === lastScanSig) return; // only log when the picture changes
    lastScanSig = sig;
    console.log(
      `[Tailrd scan] frame=${isTopFrame ? "TOP" : "child"} url=${location.href.slice(0, 90)}`,
      {
        rawControlsSeen: rawControls,
        detectedFields: fields.length,
        profileLoaded, // did a profile reach the scanner?
        withProposedValue: withValue, // fields the profile produced a value for
        wouldAutoSelect, // fields the Autofill button would act on (drives enable/count)
        iframesOnPage: iframes.length,
        crossOriginIframes: crossOrigin,
      }
    );
  } catch {
    // diagnostics must never break scanning
  }
}

/** Turn a reconciliation report into the popup's per-field outcome shape. */
function reportToOutcome(r: FieldReport): { fieldId: string; ok: boolean; reason?: string } {
  if (r.ok) return { fieldId: r.fieldId, ok: true };
  return { fieldId: r.fieldId, ok: false, reason: r.reason ?? "Could not fill. Please check manually" };
}

/**
 * Resolve once the page DOM has been structurally quiet for `quietMs`, or after
 * `capMs` regardless. React ATS (Workday) mount a loading skeleton and re-render
 * the form section repeatedly as they hydrate, scanning/filling during that
 * churn captures throwaway controls that detach mid-write ("Field no longer
 * found", the #1 live Workday failure) or get their values wiped by the next
 * render. Waiting for quiescence lets us scan and fill the real, settled form.
 * Bounded, so a page that never fully settles (animations, polling widgets)
 * still proceeds. Aborts immediately when the fill is cancelled.
 */
function waitForDomSettle(signal?: AbortSignal, quietMs = 400, capMs = 3000): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    let quiet: ReturnType<typeof setTimeout>;
    let cap: ReturnType<typeof setTimeout>;
    let obs: MutationObserver;
    const done = (): void => {
      obs.disconnect();
      clearTimeout(quiet);
      clearTimeout(cap);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const bump = (): void => {
      clearTimeout(quiet);
      quiet = setTimeout(done, quietMs);
    };
    obs = new MutationObserver(bump);
    obs.observe(document.documentElement, { childList: true, subtree: true });
    quiet = setTimeout(done, quietMs);
    cap = setTimeout(done, capMs);
    signal?.addEventListener("abort", done, { once: true });
  });
}

/**
 * Log why an advance click left the page put, the real blocker on live ATS
 * (Workday et al.): a visible validation error, or a required field the fill
 * couldn't complete, is what makes a page refuse to advance (for the extension
 * AND a manual click). Best-effort; never throws. deepQueryAll already skips the
 * extension's own UI.
 */
function logStuckDiagnostics(): void {
  try {
    const errors = deepQueryAll(
      document.body,
      '[role="alert"], [aria-live="assertive"], [aria-invalid="true"], [class*="error" i], ' +
        `[data-automation-id*="${WD_ERROR_FRAGMENT}" i]`
    )
      .filter((el) => isVisible(el))
      .map((el) => cleanText(el.textContent))
      .filter((t) => t.length > 0 && t.length < 200);

    const emptyRequired = deepQueryAll(
      document.body,
      'input[required], select[required], textarea[required], [aria-required="true"]'
    )
      .filter((el) => isVisible(el))
      .filter((el) => {
        const input = el as HTMLInputElement;
        if (input.type === "checkbox" || input.type === "radio") return !input.checked;
        return !(input.value && input.value.trim());
      })
      .map(
        (el) =>
          cleanText(el.getAttribute("aria-label")) ||
          cleanText(el.closest("label")?.textContent) ||
          (el as HTMLInputElement).name ||
          el.getAttribute("data-automation-id") ||
          "(unlabeled)"
      );

    console.log(
      "[Tailrd stuck] page did not advance:",
      "visible errors:", JSON.stringify([...new Set(errors)].slice(0, 6)),
      "| empty required fields:", JSON.stringify([...new Set(emptyRequired)].slice(0, 10))
    );
  } catch (err) {
    console.log("[Tailrd stuck] diagnostic failed:", err instanceof Error ? err.message : err);
  }
}

function initialize(): void {
  let registry: Map<string, RuntimeControl> = new Map();
  let lastAdapter: SiteAdapter | null = null;
  let lastFields: DetectedField[] = [];
  let lastScope: HTMLElement | null = null;
  let flowController: FlowController | null = null;
  /** Bumped on every Stop so an in-flight initial fill can detect it lost the
   *  race and must not start a controller. See onAutofill / onFlowStop. */
  let flowGeneration = 0;
  /** The panel's picked upload résumé for this flow (auto-attach preference). */
  let flowResumeId: number | null = null;
  /** Settings' "turn filled pages by themselves", read when a flow starts. */
  let flowAutoContinue = true;
  // A login wall we have no credentials for, pauses the flow until it clears.
  let accountBlocked = false;
  // Submit tracking: bound to the terminal (submit) button once the flow reaches
  // it, so the application is recorded when the USER submits. Never auto-clicked.
  let submitTracker: SubmitTrackerHandle | null = null;
  let trackedButton: HTMLElement | null = null;
  let lastRecordedUrl: string | null = null;
  // Aborts an in-flight fill the instant the user hits Stop (or a new flow
  // supersedes this one), so a running fillOnce stops writing promptly instead
  // of running to completion, Jobright's CANCEL_AUTO_FILL parity.
  let flowAbort: AbortController | null = null;
  // Fields the terminal re-scan found no longer holding what was written, the
  // framework reverted them after the write verified. They are asked about
  // again even though the control is not empty, because "holds SOMETHING" is
  // not "holds the answer"; see selectAnswerGaps.
  let lastRevertedIds: ReadonlySet<string> = new Set();
  /** What the last fill on THIS page wrote, kept so the page can be re-audited
   *  later, at the page turn, which is the last moment it is observable. */
  let lastFill: {
    intended: Map<string, string>;
    provenance: Map<string, FieldProvenance>;
    wroteOk: Set<string>;
    dropped: DroppedAnswerLike[];
  } | null = null;
  // Retained so MutationObserver rescans can recompute proposed values.
  let lastProfile: UserApplicationProfile | null = null;
  let lastFillEEO = false;
  let observer: MutationObserver | null = null;
  // Guards maybeResumeFlow against re-entrancy: the observer can fire several
  // rescans before FLOW_STATE_GET resolves, which would otherwise start two
  // controllers from the same persisted state.
  let resumeInFlight = false;
  let overlayShown = false;
  // The top frame has adopted a form that lives in a child frame; its own local
  // scans must then never overwrite the panel with the (formless) top-frame DOM.
  let adoptedRemote = false;
  // A persisted flow is live for this tab (set async at init). A flow is
  // user-started, so the panel may always mount for it, mid-flow pages
  // (account walls, generic page-1 forms on unknown ATSes) carry no
  // job-application field evidence of their own.
  let flowActiveHint = false;
  // The user opened the panel via the toolbar (TOGGLE_PANEL). Keeps scan
  // updates flowing to a panel that never auto-mounted (no job evidence), so
  // profile-enriched fields still reach it. Deliberately NOT overlayShown:
  // a child frame's announced form may still adopt over this panel.
  let panelOpenedManually = false;

  // The adopted child frame's fields + proxy callbacks (top frame only), kept
  // separate from this frame's own scan so local scans never clobber the panel.
  let remoteFields: DetectedField[] = [];
  let remoteCallbacks: OverlayCallbacks | null = null;
  // When this frame is a child that owns the form, it has no panel of its own,
  // it pushes field changes up to the top frame's panel instead of mounting one.
  let actingAsRemoteHost = false;

  /** Push current fields to wherever the panel lives (local overlay, or, when
   *  this frame is a child form-host, the top frame's panel). */
  function reportFields(carryReverted = false): void {
    if (actingAsRemoteHost) {
      postToRuntime({
        type: "RELAY_TO_TOP",
        payload: {
          type: "REMOTE_FIELDS_UPDATED",
          fields: lastFields,
          ...(carryReverted ? { reverted: [...lastRevertedIds] } : {}),
        },
      });
      return;
    }
    maybeShowOrUpdateOverlay(carryReverted);
  }

  // One reconciliation engine per frame, created on first fill. It keeps a
  // MutationObserver alive afterwards to correct post-fill drift.
  let engine: AutofillReconciler | null = null;
  const getEngine = (): AutofillReconciler => {
    if (!engine) engine = new AutofillReconciler({ root: document });
    return engine;
  };

  // After the extension is reloaded/updated this content script is orphaned:
  // every runtime message throws "Extension context invalidated". The messaging
  // helpers detect that and run this once so we stop the observers that would
  // otherwise re-fire (and re-throw) on every subsequent page mutation.
  onExtensionContextInvalidated(() => {
    try {
      observer?.disconnect();
    } catch {
      // observer already gone
    }
    observer = null;
    try {
      engine?.dispose();
    } catch {
      // reconciler already disposed
    }
    try {
      submitTracker?.dispose();
    } catch {
      // tracker already disposed
    }
    // Stop any parked flow: it can no longer persist state or reach the
    // background, so leaving it "running" only strands the user at a gate that
    // will never do anything.
    try {
      flowController?.stop();
    } catch {
      // controller already unwound
    }
    flowController = null;
    // The panel is still on screen and still looks usable, tell the truth.
    try {
      if (isTopFrame) showReloadRequired();
    } catch {
      // overlay never mounted on this page
    }
  });

  const isTopFrame = ((): boolean => {
    try {
      return window.self === window.top;
    } catch {
      return false; // cross-origin parent → we are in an iframe
    }
  })();

  // The job's country, city and company, read once per URL: the question
  // resolver answers "authorized to work in THIS country?" for the job's
  // country, and "located here, or would you relocate?" for its city.
  let resolveContextUrl = "";
  let pagePlaceFound = false;
  function refreshResolveContext(): void {
    if (resolveContextUrl === location.href) {
      // Workable renders the posting's header after the first scan (Mindex,
      // live 2026-10-03: "onsite at our Rochester office?" never saw
      // Rochester, New York). Read again until the page names a place; a place
      // adopted from outside stays unless the page itself names one.
      if (pagePlaceFound) return;
      try {
        const late = detectJobPlace(document);
        if (late.country || late.places?.length) {
          pagePlaceFound = true;
          setResolveContext({ jobCountry: late.country ?? getResolveContext().jobCountry, jobCity: late.city, jobPlaces: late.places ?? null });
        }
      } catch {
        // The page's own place stays unknown; nothing to change.
      }
      return;
    }
    resolveContextUrl = location.href;
    let place: JobPlace = { country: null, city: null };
    try {
      place = detectJobPlace(document);
      setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: sanitizeCompany(extractJobIdentity().company) });
    } catch {
      setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
    }
    pagePlaceFound = Boolean(place.country || place.places?.length);
    if (!place.country) void adoptOutsidePlace(resolveContextUrl);
  }

  /** The place this frame cannot see, from the page around it (an embedded
   *  form) or the posting page before it (a Workday application step). */
  async function adoptOutsidePlace(forUrl: string): Promise<void> {
    const adopted = (!isTopFrame && (await adoptTopFramePlace(forUrl))) || (await adoptCarriedJobCountry(forUrl));
    if (adopted && lastFields.length > 0) {
      runScan(); // re-resolve the place-dependent answers
      reportFields();
    }
  }

  /** An application step that shows no job location (Workday's) takes the
   *  place its posting page showed, same host, captured within 6 hours. */
  async function adoptCarriedJobCountry(forUrl: string): Promise<boolean> {
    try {
      const cached = await getLastJobContext();
      if (!cached?.country || resolveContextUrl !== forUrl) return false;
      const from = new URL(cached.url);
      if (from.host !== location.host) return false;
      // The SAME job only: an application step continues its posting's path
      // (Workday ".../job/Toronto-ON/Engineer_R1" -> ".../Engineer_R1/apply").
      // Every Lever company shares jobs.lever.co, and Arc'teryx's form took the
      // PREVIOUS posting's country (US): "require sponsorship?" got Yes for a
      // job in Canada (live 2026-10-03).
      const posting = from.pathname.replace(/\/+$/, "");
      if (posting.split("/").filter(Boolean).length < 2 || !location.pathname.startsWith(posting)) return false;
      setResolveContext({ jobCountry: cached.country, jobCity: cached.city ?? null });
      return true;
    } catch {
      return false; // No cache / storage unavailable: the place stays unknown.
    }
  }

  /**
   * A form in an iframe (Greenhouse's embed on brex.com, live 2026-10-03)
   * shows no posting; the page around it states where the job is. Asked of
   * this tab's top frame, so it is the page the user is on now.
   */
  async function adoptTopFramePlace(forUrl: string): Promise<boolean> {
    if (isTopFrame) return false;
    try {
      const resp = await sendToBackground<JobPlaceResponse>({ type: "TOP_JOB_PLACE_GET" });
      if (!resp?.ok || !resp.country || resolveContextUrl !== forUrl) return false;
      setResolveContext({
        jobCountry: resp.country,
        jobCity: resp.city ?? null,
        ...(getResolveContext().company ? {} : { company: resp.company ?? "" }),
      });
      return true;
    } catch {
      return false; // Top frame not ready / background asleep.
    }
  }

  /** Before a fill: an embedded form still without the job's country asks the
   *  page around it again (that page may have loaded after this frame). */
  async function ensureJobPlace(): Promise<void> {
    if (isTopFrame || getResolveContext().jobCountry) return;
    await adoptTopFramePlace(resolveContextUrl);
  }

  /** A field's label for diagnostics (labels only, never the values). */
  function labelOfField(id: string): string {
    return (lastFields.find((f) => f.id === id)?.label ?? id).replace(/\s+/g, " ").slice(0, 48);
  }

  let formHintUrl = "";
  function runScan(): ScanResponse {
    refreshResolveContext();
    let result = scanPage(lastProfile, lastFillEEO);
    // No job country on the page: the form's own work-authorization questions
    // may all name one (formCountryHint). Once per URL; a stated place that
    // arrives later (top frame, carried posting) still overrides it.
    // Marked done only once a hint is found: Workable renders its questions
    // after the first scan, and the hint was never looked for again (Mindex).
    if (!getResolveContext().jobCountry && formHintUrl !== location.href) {
      const hint = formCountryHint(result.fields.map((f) => f.label));
      if (hint) {
        formHintUrl = location.href;
        setResolveContext({ jobCountry: hint });
        result = scanPage(lastProfile, lastFillEEO);
      }
    }
    registry = result.registry;
    lastAdapter = result.adapter;
    lastFields = result.fields;
    lastScope = result.scopeEl;
    logScanDiagnostics(isTopFrame, result.fields, lastProfile !== null);
    return {
      ok: true,
      url: location.href,
      frameToken: FRAME_TOKEN,
      fields: result.fields,
    };
  }

  /** Rescan once the page is quiet, keeping the reconciler on the fresh controls. */
  async function rescanSettled(signal?: AbortSignal): Promise<void> {
    await waitForDomSettle(signal);
    runScan();
    engine?.updateRegistry(registry);
  }

  // What the page guessed from the visitor's location (Workable's Address,
  // pageGuesses.ts): read once per posting, then rescanned so the panel
  // offers the box.
  let pageGuessesFor = "";
  let pageGuessesLoad: Promise<void> = Promise.resolve();
  function ensurePageGuesses(): Promise<void> {
    const code = workableShortcode(location.href);
    if (!code || code === pageGuessesFor) return pageGuessesLoad;
    pageGuessesFor = code;
    pageGuessesLoad = loadWorkableGuesses(location.href).then(() => {
      if (lastFields.length === 0) return;
      runScan();
      engine?.updateRegistry(registry);
      reportFields();
    });
    return pageGuessesLoad;
  }

  // ---- In-page overlay -------------------------------------------------------

  function recognizedCount(fields: DetectedField[]): number {
    return fields.filter((f) => f.category !== "unknown").length;
  }

  /** One field's result from a non-reconciler pass (combobox / driver / adapter
   *  op), carrying the failure reason so telemetry records WHY it failed. */
  type PassOutcome = { fieldId: string; ok: boolean; reason?: string };

  /**
   * Fill custom ARIA dropdowns one at a time by opening the listbox and clicking
   * the matching option (comboboxEngine). Sequential so two menus never fight,
   * and deliberately NOT handed to the reconciler, re-driving a dropdown on
   * every mutation is the churn we avoid. Returns popup-style outcomes.
   */
  async function fillComboboxTargets(
    targets: { fieldId: string; value: string }[],
    signal?: AbortSignal
  ): Promise<{ outcomes: PassOutcome[]; reask: ReaskCandidate[] }> {
    const outcomes: PassOutcome[] = [];
    const reask: ReaskCandidate[] = [];
    for (const t of targets) {
      if (signal?.aborted) break; // Stop pressed, don't open more menus
      const control = await liveControlFor(t.fieldId, (id) => registry.get(id), () => rescanSettled(signal));
      const el = control?.el;
      if (!el) {
        outcomes.push({ fieldId: t.fieldId, ok: false, reason: "Field no longer found. Rescan the page" });
        continue;
      }
      // A street address box with suggestions takes the street as typed when
      // none of them is it (Paylocity's "Address Line 1", live 2026-10-03).
      const freeText = lastFields.find((f) => f.id === t.fieldId)?.category === "addressStreet";
      const res = await fillAriaCombobox(el, t.value, { multi: control?.multi, placeHint: placeHintFor(t.fieldId), freeText });
      // Carry the specific reason (couldn't-open / no-match / didn't-commit) into
      // telemetry, otherwise a dropdown failure is logged with an empty reason.
      outcomes.push({ fieldId: t.fieldId, ok: res.filled, reason: res.reason });
      if (!res.filled && res.options) reask.push({ fieldId: t.fieldId, options: res.options });
    }
    return { outcomes, reask };
  }

  /** Fill react-select / Workday fields via the MAIN-world driver. */
  async function fillDriverTargets(
    targets: { fieldId: string; value: string }[],
    signal?: AbortSignal
  ): Promise<{ outcomes: PassOutcome[]; reask: ReaskCandidate[] }> {
    const outcomes: PassOutcome[] = [];
    const reask: ReaskCandidate[] = [];
    for (const t of targets) {
      if (signal?.aborted) break; // Stop pressed, don't drive more fields
      const control = await liveControlFor(t.fieldId, (id) => registry.get(id), () => rescanSettled(signal));
      if (!control) { outcomes.push({ fieldId: t.fieldId, ok: false, reason: "Field no longer found. Rescan the page" }); continue; }
      if (!control.driver) { outcomes.push({ fieldId: t.fieldId, ok: false }); continue; }
      // Already displaying this value (an earlier pass, or the user, committed
      // it), driving it again would visibly erase and re-pick the selection.
      if (control.el && comboboxDisplaysValue(control.el, t.value)) {
        outcomes.push({ fieldId: t.fieldId, ok: true });
        continue;
      }
      // A multi-value widget (skills) takes one chip per item, the driver sets
      // a single value, so route it to the combobox engine's chip loop instead.
      if (control.multi && control.el) {
        const res = await fillAriaCombobox(control.el, t.value, { multi: true });
        outcomes.push({ fieldId: t.fieldId, ok: res.filled, reason: res.reason });
        if (!res.filled && res.options) reask.push({ fieldId: t.fieldId, options: res.options });
        continue;
      }
      const res = await driveField(t.fieldId, t.value, control.driver, { placeHint: placeHintFor(t.fieldId) });
      if (res.ok || !control.el) {
        outcomes.push({ fieldId: t.fieldId, ok: res.ok, reason: res.ok ? undefined : res.reason });
        continue;
      }
      // react-select before v5 has no ARIA the fallback below can open
      // ("Couldn't open the dropdown" on Epic Games' School and Degree, live
      // 2026-10-05): the options the driver saw go to the re-ask instead.
      if (res.options?.length && !control.el.hasAttribute("role")) {
        outcomes.push({ fieldId: t.fieldId, ok: false, reason: `No option matches "${t.value.length > 40 ? t.value.slice(0, 40) + "…" : t.value}"` });
        reask.push({ fieldId: t.fieldId, options: res.options });
        continue;
      }
      // Driver miss: best-effort ARIA fallback: may fill, or harvest options.
      const fb = await fillAriaCombobox(control.el, t.value, { multi: control.multi, placeHint: placeHintFor(t.fieldId) });
      outcomes.push({ fieldId: t.fieldId, ok: fb.filled, reason: fb.reason });
      if (!fb.filled && fb.options) reask.push({ fieldId: t.fieldId, options: fb.options });
    }
    return { outcomes, reask };
  }

  /**
   * The applicant's full place ("Toronto, ON, Canada") for a city/location
   * dropdown, so a suggestion list is chosen from as PLACES (Toronto, Ontario,
   * never Toronto, Ohio). Only high-confidence parts; undefined when the
   * profile does not pin at least two of city / region / country.
   */
  function placeHintFor(fieldId: string): string | undefined {
    const f = lastFields.find((x) => x.id === fieldId);
    if (!f || !lastProfile || (f.category !== "addressCity" && f.category !== "location")) return undefined;
    const loc = profileFacts(lastProfile).location;
    const parts = [
      isHigh(loc.city) ? loc.city.value : "",
      isHigh(loc.region) ? loc.region.value.code : "",
      isHigh(loc.country) ? loc.country.value.name : "",
    ].filter(Boolean);
    return parts.length >= 2 ? parts.join(", ") : undefined;
  }

  /** Whether a tracked field is a custom ARIA dropdown (filled by comboboxEngine). */
  function isComboboxField(fieldId: string): boolean {
    return registry.get(fieldId)?.controlType === "combobox";
  }

  /** Whether a tracked field is filled via the MAIN-world driver (react-select/Workday). */
  const isDriverField = (fieldId: string): boolean => Boolean(registry.get(fieldId)?.driver);

  /** Dedupe DetectedFields by id (first wins). */
  function dedupeById(fields: DetectedField[]): DetectedField[] {
    const seen = new Set<string>();
    const out: DetectedField[] = [];
    for (const f of fields) {
      if (!seen.has(f.id)) { seen.add(f.id); out.push(f); }
    }
    return out;
  }

  /**
   * Fill a list of {fieldId,value} through the same path as onAutofill: the site
   * adapter gets first refusal, then react-select/Workday drivers, custom ARIA
   * dropdowns, and the reconciler for the rest. `merge` adds to the running
   * reconciler state (a later pass); otherwise it starts a fresh run.
   */
  async function fillItems(
    items: { fieldId: string; value: string }[],
    merge: boolean,
    signal?: AbortSignal
  ): Promise<{ reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[]; reask: ReaskCandidate[] }> {
    if (items.length === 0 && merge) return { reports: [], outcomes: [], reask: [] };
    const { opOutcomes, remaining } = await runAdapterOperations(lastAdapter, items, (id) => registry.get(id));
    const driverTargets = remaining.filter((it) => isDriverField(it.fieldId));
    const comboTargets = remaining.filter((it) => !isDriverField(it.fieldId) && isComboboxField(it.fieldId));
    const reconTargets = remaining.filter((it) => !isDriverField(it.fieldId) && !isComboboxField(it.fieldId));
    const reports = merge
      ? reconTargets.length
        ? await getEngine().addTargets(reconTargets, registry, signal)
        : []
      : await getEngine().run(reconTargets, registry, signal);
    const combo = comboTargets.length
      ? await fillComboboxTargets(comboTargets, signal)
      : { outcomes: [], reask: [] };
    const driver = driverTargets.length
      ? await fillDriverTargets(driverTargets, signal)
      : { outcomes: [], reask: [] };
    // A <select> that failed on "No option matches" re-reads its options fresh
    // (dependent dropdowns (Country → State) repopulate after earlier fills).
    const reask: ReaskCandidate[] = [...combo.reask, ...driver.reask];
    for (const r of reports) {
      if (r.ok || !r.reason?.startsWith("No option matches")) continue;
      const control = registry.get(r.fieldId);
      if (control?.controlType === "select" && control.el?.isConnected) {
        const options = selectOptions(control.el as HTMLSelectElement);
        if (options.length > 0) reask.push({ fieldId: r.fieldId, options });
      }
    }
    return { reports, outcomes: [...combo.outcomes, ...driver.outcomes, ...opOutcomes], reask };
  }

  /**
   * One full fill pass over the current step. `ids === null` fills the default
   * selection (used by the flow for steps 2+ where there is no panel click).
   * Preserves the Task-7 single re-ask round. Every backend answer fills
   * silently (no review gate). Returns the step tally.
   */
  /** True when the control for `id` currently holds no user-visible value. */
  function controlIsEmpty(id: string): boolean {
    return holdsNoAnswer(registry.get(id), readComboboxValue);
  }

  /**
   * Silently fill the user's own custom autofill fields, the labelled
   * question/answer pairs they authored themselves (autofillExtras), for
   * questions the profile has no slot for. Matched by exact-normalized label,
   * with NO UI: the old interactive "missing information" modal was removed, so
   * unknown fields are left for the user and the flow's advance gate surfaces
   * any empty required ones. Returns the fills so the caller can fold them into
   * its tally.
   */
  async function refillCustomFields(signal?: AbortSignal): Promise<{
    reports: FieldReport[];
    outcomes: { fieldId: string; ok: boolean }[];
  }> {
    const empty = { reports: [], outcomes: [] };

    let localFill: { reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[] } = empty;
    try {
      const stored = customFieldAnswers(await getExtras());
      if (stored.size > 0) {
        const storedTargets = lastFields
          .filter((f) => f.fillable && f.proposedValue === null && controlIsEmpty(f.id))
          .map((f) => ({ fieldId: f.id, value: stored.get(normalize(f.label)) ?? "" }))
          .filter((t) => t.value);
        if (storedTargets.length > 0) {
          const r = await fillItems(storedTargets, true, signal);
          localFill = { reports: r.reports, outcomes: r.outcomes };
        }
      }
    } catch {
      // Storage unavailable: nothing to refill.
    }

    return localFill;
  }

  /**
   * Click "Add another" to create the work-experience / education rows the
   * profile needs (the resolver already fills row N from experience[N]). Re-finds
   * the button and re-counts after each click so it survives the row re-render,
   * caps rows, and stops the moment a click adds nothing (wrong button / limit).
   */
  async function expandRepeatingSections(signal?: AbortSignal): Promise<void> {
    if (!lastProfile) return;
    for (const kind of SECTION_KINDS) {
      const needed = Math.min(rowsNeeded(lastProfile, kind), MAX_ROWS);
      if (needed === 0) continue; // nothing to add for this section
      // Drive by needed rows, not "≥1 present", some ATS show an empty section
      // with just an "Add Work Experience" button. findAddButton falls back to a
      // section-specific button (unambiguous text) when there are no fields to
      // scope a generic "Add", so it can't mis-click a neighbouring section.
      for (let guard = 0; guard <= MAX_ROWS; guard++) {
        if (signal?.aborted) return;
        const present = rowsPresent(lastFields, kind);
        if (present >= needed) break;
        const btn = findAddButton(lastFields, kind, (id) => registry.get(id)?.el);
        if (!btn) break;
        activateElement(btn);
        await waitForDomSettle(signal);
        runScan();
        if (rowsPresent(lastFields, kind) <= present) break; // click added no row, stop
      }
    }
  }

  /** True when the page has a location cascade (Country + State/City), the case
   *  where a child dropdown only offers valid options once its parent is set. */
  function hasLocationCascade(): boolean {
    return (
      lastFields.some((f) => f.category === "country") &&
      lastFields.some((f) => f.category === "addressState" || f.category === "addressCity")
    );
  }

  /**
   * Cascading dropdowns (Country → State → City): a child only lists valid
   * options after its parent is filled, so its first attempt found nothing and
   * left proposedValue null. Now that parents are set, settle → rescan (which
   * re-reads the repopulated options and re-resolves proposedValue against them)
   * → re-fill the still-empty children. Two rounds cover the country→state→city
   * chain; stops as soon as a round fills nothing.
   */
  async function retryDependentDropdowns(
    signal?: AbortSignal
  ): Promise<{ reports: FieldReport[]; outcomes: PassOutcome[] }> {
    const reports: FieldReport[] = [];
    const outcomes: PassOutcome[] = [];
    if (!hasLocationCascade()) return { reports, outcomes };
    for (let round = 0; round < 2; round++) {
      if (signal?.aborted) break;
      await waitForDomSettle(signal);
      runScan();
      engine?.updateRegistry(registry);
      const retry = lastFields
        .filter(
          (f) =>
            // Only the LOCATION cascade re-fills (its stated purpose): pulling
            // every "empty-looking" dropdown back in re-drives fields whose
            // committed value the empty-check simply couldn't read (the
            // fill/remove/fill churn on school pickers and the like).
            (f.category === "country" ||
              f.category === "addressState" ||
              f.category === "addressCity" ||
              f.category === "location") &&
            (f.controlType === "select" || f.controlType === "combobox") &&
            f.fillable &&
            f.proposedValue !== null &&
            controlIsEmpty(f.id)
        )
        .map((f) => ({ fieldId: f.id, value: f.proposedValue as string }));
      if (retry.length === 0) break;
      const r = await fillItems(retry, true, signal);
      reports.push(...r.reports);
      outcomes.push(...r.outcomes);
      if (!r.reports.some((rep) => rep.ok) && !r.outcomes.some((o) => o.ok)) break;
    }
    return { reports, outcomes };
  }

  async function fillOnce(
    ids: string[] | null,
    signal?: AbortSignal,
    knownAtClick: ReadonlySet<string> | null = null,
    afterEntry = false
  ): Promise<StepTally> {
      if (signal?.aborted) return { ok: 0, fail: 0, total: 0 };
      // Let a React ATS finish hydrating before we scan+fill: filling a form
      // that is still swapping in its real fields captures throwaway controls
      // that detach mid-write. Wait for the DOM to go quiet, then rescan so we
      // operate on the settled form. A stable page clears the quiet window
      // immediately; the flow path (ids === null) re-derives its selection from
      // this fresh scan, so newly-mounted real fields are the ones filled.
      await waitForDomSettle(signal);
      if (signal?.aborted) return { ok: 0, fail: 0, total: 0 };
      await ensureJobPlace();
      await Promise.race([ensurePageGuesses(), new Promise((resolve) => setTimeout(resolve, 2500))]);
      runScan();
      // A form an "Apply" click opens can still sit hidden while the site loads
      // (hiddenForm.ts). On the page the flow's entry click just opened, or on a
      // user's click that picked nothing (they pressed Autofill right after
      // opening the form themselves; the flow would otherwise click "Apply"
      // again and toggle the form shut), wait for it to show (bounded; free when
      // the page's form isn't hidden) if there is nothing to fill yet, or next
      // to nothing is rendered: a stray field outside the hidden form must not
      // end the wait (BambooHR). Ordinary wizard steps never wait.
      if (afterEntry || (ids !== null && ids.length === 0)) {
        const nothingYet = defaultSelectedIds(lastFields).size === 0;
        if (nothingYet || controlVisibility(document).shown <= 2) {
          const found = await waitForFormReveal(
            {
              measure: () => controlVisibility(document),
              rescan: async () => {
                await waitForDomSettle(signal);
                runScan();
                return nothingYet
                  ? defaultSelectedIds(lastFields).size > 0
                  : !formLooksHidden(controlVisibility(document));
              },
            },
            signal
          );
          if (found) console.log("[Tailrd flow] hidden form shown, rescanned");
        }
      }
      if (signal?.aborted) return { ok: 0, fail: 0, total: 0 };
      // Create the extra work-experience / education rows the profile needs before
      // filling, so a candidate with several jobs doesn't get only the first row.
      await expandRepeatingSections(signal);
      engine?.updateRegistry(registry);
      // The picked ids, plus rows this pass created itself ("Add education")
      // that the user never saw to deselect.
      const wanted = fillSelection(lastFields, ids, knownAtClick);
      const selected = lastFields.filter(
        (f) => wanted.has(f.id) && f.fillable && f.proposedValue !== null
      );

      // What each field was asked to hold, and where that came from. Recorded
      // as the passes run so the terminal re-scan can diff observed page state
      // against intent, and so telemetry can name the tier and pass responsible
      // for a value, the attribution the old failure-only record could not make.
      // Phase timings. "The autofill takes exceptionally long" was reported on
      // 2026-08-13 with nothing in the record to support it; the cause (45s of
      // futile OpenAI retries inside the backend phase) had to be found by
      // reading timestamps in Vercel logs. These make that a query.
      const fillStartedAt = performance.now();
      const phase = { scanMs: 0, localMs: 0, backendMs: 0, reaskMs: 0 };
      const since = (t: number): number => Math.round(performance.now() - t);
      // Started here, awaited at the end: an account that never opted in must
      // not pay a round trip for it, and one that did must not build captures
      // before we know.
      const diagnosticCheck = isDiagnosticMode();

      const intended = new Map<string, string>();
      const provenance = new Map<string, FieldProvenance>();
      const droppedByBackend = new Map<string, DroppedAnswerLike>();
      type FillTarget = { fieldId: string; value: string };
      const noteIntent = (
        targets: FillTarget[],
        prov: FieldProvenance | ((fieldId: string) => FieldProvenance)
      ): FillTarget[] => {
        for (const t of targets) {
          intended.set(t.fieldId, t.value);
          provenance.set(t.fieldId, typeof prov === "function" ? prov(t.fieldId) : prov);
        }
        return targets;
      };
      const noteDrops = (drops: DroppedAnswer[] | undefined): void => {
        for (const d of drops ?? []) {
          droppedByBackend.set(d.id, { fieldId: d.id, reason: d.reason, source: d.source });
        }
      };

      // Phase A: deterministic profile fields fill instantly (local fast-path).
      const localStartedAt = performance.now();
      const route = planFillRoute(selected, AUTOFILL_CONFIDENCE_THRESHOLD);
      // A react-datepicker box (Workable's dates) is typed last, after every
      // dropdown: on Saalex's form, a State chosen after its dates were typed
      // sent Workable's form into an endless re-render that froze the tab
      // (live 2026-10-05).
      const pickerBox = (fieldId: string): boolean => Boolean(registry.get(fieldId)?.el?.closest(".react-datepicker-wrapper"));
      const lateTargets = route.localTargets.filter((t) => pickerBox(t.fieldId));
      let localFill = await fillItems(
        noteIntent(route.localTargets.filter((t) => !pickerBox(t.fieldId)), { tier: "profile" }), false, signal
      );
      phase.localMs = since(localStartedAt);

      // Phase B: judgment fields answered by the backend (primary), deduped by the
      // session cache; also the eligible EMPTY fields (today's AI candidates). The
      // local proposedValue is the fallback so a judgment field never regresses when
      // the backend is unavailable.
      const backendFields = dedupeById([...route.backendFields, ...aiFillCandidates(lastFields)]);
      let aiFill: { reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[]; reask: ReaskCandidate[] } =
        { reports: [], outcomes: [], reask: [] };
      let fallbackFill: { reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[]; reask: ReaskCandidate[] } =
        { reports: [], outcomes: [], reask: [] };
      let optionFill: { reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[]; reask: ReaskCandidate[] } =
        { reports: [], outcomes: [], reask: [] };
      const backendStartedAt = performance.now();
      if (backendFields.length > 0 && !signal?.aborted) {
        const { hits, misses } = splitByCache(backendFields);
        // Harvest real options for lazy dropdowns BEFORE asking the AI, so its
        // first answer is constrained to what the widget actually offers
        // (react-select / Workday mount their option list only when opened).
        for (const f of misses) {
          if (signal?.aborted) break;
          const control = registry.get(f.id);
          if (!needsOptionHarvest(f, Boolean(control?.driver)) || !control?.el) continue;
          // Short open budget: a widget that hasn't mounted its menu in 600ms
          // isn't going to, the default 1.5s (×2 with the re-open) per field
          // was seconds of visible dead time before the AI round even started.
          const harvested = await harvestComboboxOptions(control.el, { openWaitMs: 600 }).catch(
            () => undefined
          );
          if (harvested && harvested.length > 0) f.options = harvested;
        }
        // With the real options known, answer on device whatever no longer needs
        // the AI (a default or profile answer that needed the option list).
        const optionTargets: { fieldId: string; value: string }[] = [];
        const askBackend: typeof misses = [];
        for (const f of misses) {
          const v = f.options?.length
            ? resolveWithOptions(f, registry, lastProfile, lastAdapter, lastFillEEO, f.options)
            : null;
          if (v) optionTargets.push({ fieldId: f.id, value: v });
          else askBackend.push(f);
        }
        if (optionTargets.length > 0) {
          optionFill = await fillItems(noteIntent(optionTargets, { tier: "profile", pass: "options" }), true, signal);
        }
        // Labels only (never values): which lazy dropdowns the device answered
        // once their options were read, and which still wait for the AI.
        if (misses.length > 0) {
          const names = (ids: string[]): string => ids.map((id) => `"${labelOfField(id)}"`).join(", ");
          const failed = optionFill.outcomes.filter((o) => !o.ok).map((o) => o.fieldId);
          console.log(
            `[Tailrd fill] answered on device after reading options: ${names(optionTargets.map((t) => t.fieldId)) || "none"}` +
              (failed.length ? `; write failed: ${names(failed)}` : "") +
              `; left for the AI: ${names(askBackend.map((f) => f.id)) || "none"}`
          );
        }
        let answers: PlannedAnswer[] = hits;
        try {
          if (askBackend.length > 0) {
            const resp = await sendToBackground<AiFillResponse>({
              type: "AI_FILL",
              fields: askBackend.map(toAiFillField),
              jobContext: extractJobContext(),
              profile: lastProfile ? toApplicantProfile(lastProfile) : undefined,
            });
            if (resp?.ok) {
              cacheAnswers(askBackend, resp.answers);
              answers = [...hits, ...resp.answers];
              noteDrops(resp.dropped);
            }
          }
        } catch {
          // Backend unavailable: the local fallback below still fills judgment fields.
        }
        const plan = planAiFill(backendFields, answers);
        const passById = new Map(answers.map((a) => [a.id, a.fillPass ?? a.source ?? ""]));
        aiFill = await fillItems(
          noteIntent(plan.simpleTargets, (id) => ({ tier: "backend", pass: passById.get(id) ?? "" })),
          true,
          signal
        );

        // Local fallback: judgment fields that had a local value but weren't answered
        // by the backend still fill from proposedValue, no regression. A single
        // checkbox with a non-boolean value is excluded: it was routed to the AI
        // precisely because that value can only fail as "Ambiguous checkbox value".
        // A dropdown whose real options were read and hold no match for the
        // value is not typed into either: "Ontario" went into Saalex's list of
        // US states, and touching that list made Workable's form re-render
        // forever (live 2026-10-05).
        const answered = new Set<string>(plan.simpleTargets.map((t) => t.fieldId));
        const fallbackTargets = route.backendFields
          .filter((f) => !answered.has(f.id) && f.proposedValue !== null)
          .filter((f) => f.controlType !== "checkbox" || isBoolish(f.proposedValue as string))
          .filter((f) => !(f.controlType === "combobox" && f.options?.length && !matchOption(f.options, (o) => o, (o) => o, f.proposedValue as string)))
          .map((f) => ({ fieldId: f.id, value: f.proposedValue as string }));
        fallbackFill = await fillItems(
          noteIntent(fallbackTargets, { tier: "profile" }), true, signal
        );
      }
      if (lateTargets.length > 0 && !signal?.aborted) {
        const late = await fillItems(noteIntent(lateTargets, { tier: "profile" }), false, signal);
        localFill = {
          reports: [...localFill.reports, ...late.reports],
          outcomes: [...localFill.outcomes, ...late.outcomes],
          reask: [...localFill.reask, ...late.reask],
        };
      }
      phase.backendMs = since(backendStartedAt);
      const reaskStartedAt = performance.now();

      // One re-ask round: choice controls whose fill missed now carry the
      // widget's REAL options, a single batched AI_FILL snaps the answers
      // ("Canada" → "Canadian"), then a merge pass drives them in.
      let reaskFill: { reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[]; reask: ReaskCandidate[] } =
        { reports: [], outcomes: [], reask: [] };
      let demoFill: { reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[]; reask: ReaskCandidate[] } =
        { reports: [], outcomes: [], reask: [] };
      let deviceReaskFill: { reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[]; reask: ReaskCandidate[] } =
        { reports: [], outcomes: [], reask: [] };
      const reaskCandidates = [...localFill.reask, ...optionFill.reask, ...aiFill.reask, ...fallbackFill.reask];
      // Sensitive (EEO) fields NEVER reach the backend, pick their closest
      // option ON-DEVICE from the harvested list. Everything else re-asks the AI.
      const sensitiveReask = reaskCandidates.filter((c) => lastFields.find((f) => f.id === c.fieldId)?.sensitive);
      const openReask = reaskCandidates.filter((c) => !lastFields.find((f) => f.id === c.fieldId)?.sensitive);
      if (sensitiveReask.length > 0 && !signal?.aborted) {
        for (const c of sensitiveReask) {
          const f = lastFields.find((x) => x.id === c.fieldId);
          if (f) f.options = c.options; // panel shows the real choices
        }
        // undefined when there is nothing to re-resolve against (no live
        // control or profile): only then does the bare matcher answer.
        const demoTargets = planSensitiveReask(lastFields, sensitiveReask, (field, options) =>
          registry.has(field.id) && lastProfile ? resolveWithOptions(field, registry, lastProfile, lastAdapter, lastFillEEO, options) : undefined
        );
        // Tier 3, on-device: these values never leave the machine, and the
        // telemetry record says only that the device answered, never what.
        if (demoTargets.length > 0) {
          demoFill = await fillItems(noteIntent(demoTargets, { tier: "device" }), true, signal);
        }
      }
      if (openReask.length > 0 && !signal?.aborted) {
        for (const c of openReask) {
          const f = lastFields.find((x) => x.id === c.fieldId);
          if (f) f.options = c.options; // panel now shows the real choices
        }
        // Anything the profile already answers is snapped to a real option HERE,
        // on device, before the backend is consulted. Free, instant, and it
        // keeps a fact the user stated from depending on the AI being reachable.
        const onDevice = planOnDeviceReask(
          lastFields,
          openReask,
          (options, value) => matchOption(options, (o) => o, (o) => o, value),
          (field, options) => resolveWithOptions(field, registry, lastProfile, lastAdapter, lastFillEEO, options)
        );
        if (onDevice.targets.length > 0) {
          deviceReaskFill = await fillItems(
            noteIntent(onDevice.targets, { tier: "profile", pass: "reask" }), true, signal
          );
        }
        const reaskFields = planReaskFields(lastFields, onDevice.remaining);
        if (reaskFields.length > 0) {
          try {
            const resp = await sendToBackground<AiFillResponse>({
              type: "AI_FILL",
              fields: reaskFields,
              jobContext: extractJobContext(),
              profile: lastProfile ? toApplicantProfile(lastProfile) : undefined,
            });
            if (resp?.ok) {
              const affected = lastFields.filter((f) => reaskFields.some((r) => r.id === f.id));
              cacheAnswers(affected, resp.answers); // overwrite the unconstrained answers
              noteDrops(resp.dropped);
              const plan = planAiFill(affected, resp.answers);
              const reaskPass = new Map(resp.answers.map((a) => [a.id, a.fillPass ?? a.source ?? ""]));
              reaskFill = await fillItems(
                noteIntent(plan.simpleTargets, (id) => ({ tier: "backend", pass: reaskPass.get(id) ?? "" })),
                true,
                signal
              );
            }
          } catch {
            // Backend unreachable: the manual-select outcomes stand.
          }
        }
      }

      // Silently fill the user's own custom autofill fields. Skipped once cancelled.
      const missingFill = signal?.aborted ? { reports: [], outcomes: [] } : await refillCustomFields(signal);

      // Cascading location dropdowns (Country → State → City) now that parents
      // are set, a no-op unless the page actually has a location cascade.
      const cascadeFill = signal?.aborted
        ? { reports: [], outcomes: [] as PassOutcome[] }
        : await retryDependentDropdowns(signal);

      // Questions the fill itself revealed. A conditional question mounts only
      // once an earlier answer is in ("Please identify your race" appears after
      // "Hispanic/Latino?" is answered; Greenhouse, Brex live 2026-10-03), so it
      // was never in this pass's selection. Rescan once and fill what is new and
      // qualifies; one pass, so a page that keeps growing cannot loop us.
      let revealFill: { reports: FieldReport[]; outcomes: { fieldId: string; ok: boolean }[]; reask: ReaskCandidate[] } =
        { reports: [], outcomes: [], reask: [] };
      if (!signal?.aborted) {
        await waitForDomSettle(signal);
        runScan();
        engine?.updateRegistry(registry);
        const revealed = lastFields.filter((f) => !intended.has(f.id) && !wanted.has(f.id) && isDefaultSelected(f));
        if (revealed.length > 0 && !signal?.aborted) {
          console.log(`[Tailrd fill] ${revealed.length} question(s) appeared during the fill, filling them`);
          revealFill = await fillItems(
            noteIntent(revealed.map((f) => ({ fieldId: f.id, value: f.proposedValue as string })), { tier: "profile", pass: "revealed" }),
            true,
            signal
          );
          // A revealed demographic dropdown whose value missed: its closest
          // option, on device (the same rule as the re-ask round above).
          const demo = planSensitiveReask(lastFields, revealFill.reask, (field, options) =>
            registry.has(field.id) && lastProfile ? resolveWithOptions(field, registry, lastProfile, lastAdapter, lastFillEEO, options) : undefined
          );
          if (demo.length > 0) {
            const again = await fillItems(noteIntent(demo, { tier: "device" }), true, signal);
            revealFill = { reports: [...revealFill.reports, ...again.reports], outcomes: [...revealFill.outcomes, ...again.outcomes], reask: [] };
          }
        }
      }

      phase.reaskMs = since(reaskStartedAt);

      const { ok, fail, total } = tallyOutcomes(
        localFill.reports,
        optionFill.reports,
        aiFill.reports,
        fallbackFill.reports,
        reaskFill.reports,
        demoFill.reports,
        deviceReaskFill.reports,
        missingFill.reports,
        cascadeFill.reports,
        revealFill.reports,
        localFill.outcomes,
        optionFill.outcomes,
        aiFill.outcomes,
        fallbackFill.outcomes,
        reaskFill.outcomes,
        demoFill.outcomes,
        deviceReaskFill.outcomes,
        missingFill.outcomes,
        cascadeFill.outcomes,
        revealFill.outcomes
      );

      const allReports = [
        ...localFill.reports, ...optionFill.reports, ...aiFill.reports, ...fallbackFill.reports,
        ...reaskFill.reports, ...demoFill.reports, ...deviceReaskFill.reports,
        ...missingFill.reports, ...cascadeFill.reports, ...revealFill.reports,
      ];
      const allOutcomes = [
        ...localFill.outcomes, ...optionFill.outcomes, ...aiFill.outcomes, ...fallbackFill.outcomes,
        ...reaskFill.outcomes, ...demoFill.outcomes, ...deviceReaskFill.outcomes,
        ...missingFill.outcomes, ...cascadeFill.outcomes, ...revealFill.outcomes,
      ];
      // Terminal re-scan: read the page back once the fill has settled, and
      // diff what it holds against what was written.
      //
      // writeEngine verifies each commit AT WRITE TIME, which proves the value
      // reached the control and no more. A framework can revert it afterwards,
      // on blur, on its own validation, or on a re-render triggered by a LATER
      // field, and a per-write check has moved on by then. This is the only
      // point where "the page actually holds the answer" can be observed.
      let observed = signal?.aborted ? [] : await observePageState(signal);
      const okIds = new Set(
        [...finalOutcomes(allReports, allOutcomes).ok]
          .filter(([, isOk]) => isOk)
          .map(([id]) => id)
      );
      lastFill = {
        intended,
        provenance,
        wroteOk: okIds,
        dropped: [...droppedByBackend.values()],
      };
      const intendedList = [...intended].map(([fieldId, value]) => ({ fieldId, value }));
      let reverted = revertedFields(intendedList, observed, okIds);
      // One recovery pass. A field the page CLEARED after a successful write was
      // wiped by a re-render a later field triggered (Greenhouse re-renders its
      // React form when a dropdown is picked; names and email went blank, Brex
      // live 2026-10-03). Once the page settles, write it once more. A value the
      // page CHANGED (reformatting, the user typing) is never fought, and one
      // pass only: a field the page keeps clearing stays reported.
      const cleared = reverted.filter((r) => r.cleared && intended.get(r.fieldId));
      if (cleared.length > 0 && !signal?.aborted) {
        await waitForDomSettle(signal);
        if (!signal?.aborted) {
          console.log(`[Tailrd fill] re-writing ${cleared.length} field(s) the page cleared: ${cleared.map((r) => `"${labelOfField(r.fieldId)}"`).join(", ")}`);
          await fillItems(
            cleared.map((r) => ({ fieldId: r.fieldId, value: intended.get(r.fieldId) as string })),
            true,
            signal
          );
          observed = signal?.aborted ? observed : await observePageState(signal);
          reverted = revertedFields(intendedList, observed, okIds);
        }
      }
      lastRevertedIds = new Set(reverted.map((r) => r.fieldId));
      if (lastRevertedIds.size > 0) {
        console.log(
          `[Tailrd fill] ${lastRevertedIds.size} field(s) no longer hold what was written, re-asking: ${[...lastRevertedIds].map((id) => `"${labelOfField(id)}"`).join(", ")}`
        );
      }
      // Push the re-scan's own view of the page (and its reverts) to the panel,
      // so the gap card is built from what the page holds rather than from what
      // the planner intended.
      if (!signal?.aborted) reportFields(true);

      // Per-field record of this page for the panel's summary, every detected
      // field, not just the attempted ones, so a skipped required question is
      // visible BEFORE the user turns the page.
      // Fire-and-forget telemetry (field labels, categories, provenance and
      // booleans only, never values) so we can see which sites/fields the
      // filler struggles with, INCLUDING the ones it reported filling and
      // didn't. Skipped on cancel.
      if (!signal?.aborted && total > 0) {
        const diagnosticOn = await diagnosticCheck;
        const telemetry = buildAutofillTelemetry(
          lastFields,
          { host: location.host, url: location.href, atsType: lastAdapter?.id ?? "" },
          {
            reports: allReports,
            outcomes: allOutcomes,
            provenance,
            intended: [...intended].map(([fieldId, value]) => ({ fieldId, value })),
            observed,
            dropped: [...droppedByBackend.values()],
            extensionVersion: extensionVersion(),
            durations: {
              scanMs: phase.scanMs,
              localMs: phase.localMs,
              backendMs: phase.backendMs,
              reaskMs: phase.reaskMs,
              totalMs: Math.round(performance.now() - fillStartedAt),
            },
            // Diagnostic capture, only for an account that turned it on. The
            // check is awaited BEFORE anything is built, so a non-diagnostic
            // account never even assembles an answer or a snapshot, let alone
            // transmits one.
            ...(diagnosticOn
              ? {
                  capture: {
                    snapshot: (fieldId: string) => {
                      const el = registry.get(fieldId)?.el;
                      if (!el) return null;
                      // A demographic field is captured as STRUCTURE ONLY. Its
                      // committed choice renders as ordinary text in the
                      // markup, and "EEO answers never leave your device" is a
                      // promise the store listing makes, which diagnostic mode
                      // does not get to revoke.
                      const sensitive = lastFields.find((f) => f.id === fieldId)?.sensitive ?? false;
                      try {
                        return {
                          dom: captureFieldDom(el, { keepValues: !sensitive, stripText: sensitive }),
                          selector: fieldSelector(el),
                          options: captureOptions(el),
                        };
                      } catch {
                        return null; // a snapshot is never worth failing a fill for
                      }
                    },
                  },
                }
              : {}),
          }
        );
        if (telemetry.totalFields > 0) {
          void sendToBackground<SimpleResponse>({ type: "RECORD_TELEMETRY", telemetry }).catch(() => {});
        }
      }

      return { ok, fail, total };
  }

  /**
   * A fresh read of every tracked control's committed value.
   *
   * Deliberately a re-SCAN rather than a read of the cached fields: a control
   * that was replaced mid-fill has a new element behind the same id, and the
   * stale reference would report the old element's value forever.
   *
   * Values stay in this frame, only presence and equality are ever reported.
   */
  async function observePageState(signal?: AbortSignal): Promise<ObservedField[]> {
    await waitForDomSettle(signal);
    if (signal?.aborted) return [];
    runScan();
    const CHOICE = new Set(["select", "combobox", "customDropdown", "radioGroup", "ariaRadioGroup", "checkboxGroup"]);
    return lastFields.map((f) => ({
      fieldId: f.id,
      value: f.currentValue ?? "",
      ...(f.controlType === "checkbox" ? { checkbox: true } : {}),
      ...(CHOICE.has(f.controlType) ? { choice: true } : {}),
    }));
  }

  /**
   * Re-audit the current page against the last fill, and record what changed.
   *
   * Called before every page turn and once when the flow finishes, a page turn
   * is the last moment the page can be observed at all, and the final page
   * never gets one. Time has passed since the fill: the user may have typed,
   * the site may have validated, and a framework may have reset a control the
   * write engine watched land. All of that is invisible from the next page.
   *
   * A record is emitted only when this pass finds reverts the fill's own
   * re-scan did not. The fill already reported the page; re-reporting an
   * unchanged page would inflate every host's pass count and say nothing new.
   */
  async function auditPageState(): Promise<void> {
    if (!lastFill || lastFill.intended.size === 0) return;
    const observed = await observePageState();
    if (observed.length === 0) return;
    const intended = [...lastFill.intended].map(([fieldId, value]) => ({ fieldId, value }));
    const reverted = revertedFields(intended, observed, lastFill.wroteOk);
    const fresh = reverted.filter((r) => !lastRevertedIds.has(r.fieldId));
    lastRevertedIds = new Set(reverted.map((r) => r.fieldId));
    reportFields(true);
    if (fresh.length === 0) return;
    console.log(
      `[Tailrd flow] page-turn audit: ${fresh.length} field(s) reverted since the fill`
    );
    const telemetry = buildAutofillTelemetry(
      lastFields,
      { host: location.host, url: location.href, atsType: lastAdapter?.id ?? "" },
      {
        reports: [],
        outcomes: [...lastFill.wroteOk].map((fieldId) => ({ fieldId, ok: true })),
        provenance: lastFill.provenance,
        intended,
        observed,
        dropped: lastFill.dropped,
      }
    );
    if (telemetry.totalFields > 0) {
      void sendToBackground<SimpleResponse>({ type: "RECORD_TELEMETRY", telemetry }).catch(() => {});
    }
  }

  /** Route a flow progress beat to wherever the panel lives (mirrors reportFields). */
  function emitFlowProgress(p: FlowProgress): void {
    // The fill/flow is over: restore the page's dialogs.
    if (p.phase === "done" || p.phase === "stopped") void setDialogSuppression(false);
    if (actingAsRemoteHost) {
      void chrome.runtime
        .sendMessage({ type: "RELAY_TO_TOP", payload: { type: "REMOTE_FLOW_PROGRESS", progress: p } })
        .catch(() => {});
    } else {
      updateFlowProgress(p);
    }
  }

  /** Attach the flow's picked résumé (or the best default) to the pending file
   *  field. Uses the attach-side helper, not the pause-side one: the upload the
   *  user most wants filled (Workday's drop zone) is not marked required, and
   *  gating here on `required` made this a success-reporting no-op. */
  async function attachPickedResume(): Promise<boolean> {
    const field = resumeFieldForAttach(lastFields, (id) => registry.get(id));
    if (!field) return true;
    const control = registry.get(field.id);
    if (!control?.el) return false;
    try {
      let resumeId = flowResumeId;
      if (resumeId == null) {
        // Spec: fall back to the primary résumé, else the most recent with a file.
        const rs = await sendToBackground<ResumesResponse>({ type: "GET_RESUMES" });
        const withFile: ResumeSummary[] = rs?.ok ? rs.resumes.filter((r) => r.hasFile) : [];
        const pick =
          withFile.find((r) => r.isPrimary) ??
          [...withFile].sort((a, b) => (b.updatedAt ?? "").localeCompare(a.updatedAt ?? ""))[0];
        resumeId = pick?.id ?? null;
      }
      if (resumeId == null) return false;
      const file = await sendToBackground<ResumeFileResponse>({ type: "DOWNLOAD_RESUME", resumeId });
      if (!file?.ok || !file.dataBase64) return false;
      const res = await injectResumeFile(control.el, base64ToFile(file.dataBase64, file.name, file.contentType));
      return res.ok;
    } catch {
      return false;
    }
  }

  /**
   * Record the application on the user's Tailrd account (Applications page) when
   * they submit. Best-effort: a signed-out user or offline backend is ignored.
   * Deduped per URL locally so a double-click doesn't send twice; the backend
   * dedupes across sessions too.
   */
  async function recordCurrentApplication(): Promise<void> {
    const url = location.href;
    if (lastRecordedUrl === url) return;
    const ctx = extractJobContext();
    const application: ApplicationLog = {
      company: ctx.company,
      role: ctx.jobTitle,
      url,
      atsType: lastAdapter?.id ?? "",
      resumeVersion: flowResumeId != null ? "uploaded" : "original",
    };
    if (!application.company && !application.role && !application.url) return;
    lastRecordedUrl = url;
    try {
      const resp = await sendToBackground<RecordApplicationResponse>({
        type: "RECORD_APPLICATION",
        application,
      });
      if (resp?.ok) {
        emitFlowProgress({
          phase: "done",
          step: 0,
          filledOk: 0,
          filledFail: 0,
          detail: "Application tracked in your Tailrd dashboard",
        });
      } else if (resp?.needsLogin) {
        lastRecordedUrl = null; // let a later attempt retry once connected
      }
    } catch {
      lastRecordedUrl = null; // transient failure, allow a retry
    }
  }

  /** Bind submit tracking to the flow's terminal button (idempotent per button). */
  function bindSubmitOnce(button: HTMLElement): void {
    if (trackedButton === button && submitTracker) return;
    submitTracker?.dispose();
    trackedButton = button;
    submitTracker = bindSubmitTracking(button, () => void recordCurrentApplication(), {
      hasBlockingValidation: () => validationMessages(lastScope ?? document.body).length > 0,
    });
  }

  /** Where the flow looks for the page turn: the scanned form's scope, or a
   *  one-question step's own form (loneFormScope: iCIMS's email step). */
  function flowScope(): HTMLElement | null {
    if (lastScope) return lastScope;
    const entries: ScopeEntry[] = [];
    for (const f of lastFields) {
      const el = registry.get(f.id)?.el;
      if (el) entries.push({ field: f, el });
    }
    return loneFormScope(entries);
  }

  function makeFlowDeps(): FlowDeps {
    return {
      fillStep: (ids, ctx) => fillOnce(ids, flowAbort?.signal, null, ctx?.afterEntry === true),
      snapshot: (): FlowSnapshot => ({
        fields: lastFields,
        scopeEl: flowScope(),
        url: location.href,
        entry: findApplyEntry(document, lastAdapter),
        accountWall: detectWall(lastScope ?? document.body) !== null,
        remoteForm: isTopFrame && adoptedRemote,
      }),
      rescan: () => {
        runScan();
        engine?.updateRegistry(registry);
      },
      findAdvance: (scope, extraAdvance) => findAdvanceButton(scope, lastAdapter, { extraAdvance }),
      advancePending: (scope, extraAdvance) => findPendingAdvance(scope, { extraAdvance }) !== null,
      findPageTerminal: () => {
        const footer = lastAdapter?.advanceButton?.(document.body);
        if (!footer) return null;
        const adv = findAdvanceButton(document.body, lastAdapter);
        return adv && adv.el === footer && adv.kind === "terminal" ? footer : null;
      },
      clickAdvance,
      onTerminal: (button) => bindSubmitOnce(button),
      accountStep: async (snap) => {
        const scope = snap.scopeEl ?? document.body;
        let wall = detectWall(scope);
        if (!wall) {
          accountBlocked = false;
          return {};
        }
        // A sign-in wall with no credential saved for this origin: we never
        // created an account here, so when the page offers a create-account
        // toggle (Workday's createAccountLink), flip to registration, the
        // default intent, matching the reference flow. A user who already has
        // an account still signs in manually during the resulting pause.
        if (wall.kind === "login" && !(await getCredential(location.origin))) {
          const toggle = findSignupToggle(scope);
          if (toggle) {
            console.log("[Tailrd flow] login wall with no saved credential, switching to create-account");
            clickAdvance(toggle);
            await new Promise((resolve) => setTimeout(resolve, 800));
            runScan();
            engine?.updateRegistry(registry);
            wall = detectWall(lastScope ?? document.body) ?? wall;
          }
        }
        const out = await runAccountWall(
          wall,
          location.origin,
          lastProfile?.email ?? "",
          (el, value) =>
            writeControl(
              { id: el.getAttribute("data-ap-field") ?? "", controlType: el.type === "password" ? "password" : "text", el },
              value
            )
        );
        // The wall owns its email field: drop the generic proposal so the
        // fill pass that follows can't overwrite the registration email with
        // the profile email, the saved pair must match what the site got.
        if (wall.emailEl?.value) {
          for (const f of lastFields) {
            if (registry.get(f.id)?.el === wall.emailEl) f.proposedValue = null;
          }
        }
        // Give the form a beat to react to the password + ticked consent before
        // the controller looks for (and clicks) Create Account, Workday enables
        // that button only once its own validation has re-run.
        if (out.filled > 0) await new Promise((resolve) => setTimeout(resolve, 500));
        accountBlocked = out.pause === "account";
        return { extraAdvance: out.extraAdvance, wall: wall.kind };
      },
      pauseReason: async (snap) => {
        if (accountBlocked && detectWall(snap.scopeEl ?? document.body)) return "account";
        accountBlocked = accountBlocked && detectWall(snap.scopeEl ?? document.body) !== null;
        if (hasUnsolvedCaptcha(document)) return "captcha";
        const scope = snap.scopeEl ?? document.body;
        if (isVerificationWall(scope)) return "verification";
        // Form-shaped blockers only apply where there IS a form. On an entry
        // page (job posting, apply-method chooser) the scan finds no fields and
        // no scope, so `scope` degrades to the whole document, and a
        // page-level alert anywhere on a Workday posting would then read as a
        // rejected form and park the flow before it ever clicks Apply. Nothing
        // has been submitted on such a page, so neither check can be meaningful.
        if (snap.fields.length === 0) return null;
        if (validationMessages(scope).length > 0) return "validation";
        // PAUSING stays required-gated: an optional upload that could not be
        // attached must never park the flow behind a wall the user cannot clear.
        if (resumeFieldNeedingFile(snap.fields, (id) => registry.get(id))) return "resume-upload";
        return null;
      },
      // ATTACHING is not: the drop zone carries no `required` marker, so this
      // is what makes the flow attach the résumé without the user clicking.
      needsResume: (snap) => resumeFieldForAttach(snap.fields, (id) => registry.get(id)) !== null,
      hasUnfilledRequired: (snap) => {
        const empty = snap.fields.filter(
          (f) => f.required && f.fillable && f.controlType !== "file" && controlIsEmpty(f.id)
        );
        // Name them: "a required field is empty" is not something anyone can act on.
        if (empty.length > 0) {
          console.log(`[Tailrd flow] required but empty: ${empty.map((f) => `«${f.label.slice(0, 60)}» (${f.controlType})`).join(", ")}`);
        }
        return empty.length > 0;
      },
      attachResume: attachPickedResume,
      setState: async (state: FlowState | null) => {
        try {
          await sendToBackground<SimpleResponse>({ type: "FLOW_STATE_SET", state });
        } catch {
          // Background asleep: the flow still runs, it just won't survive navigation.
        }
      },
      onProgress: emitFlowProgress,
      diagnoseStuck: logStuckDiagnostics,
      auditPageState,
      autoContinue: () => flowAutoContinue,
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      now: () => Date.now(),
    };
  }

  /** Read the flow's settings from the extension config; a failed read keeps
   *  the default (filled pages turn by themselves). */
  async function readFlowSettings(): Promise<void> {
    try {
      flowAutoContinue = (await getConfig()).flowAutoContinue !== false;
    } catch {
      flowAutoContinue = true;
    }
  }

  // A person who types or clicks in the page while it counts down wants to stay
  // on it: hold the countdown, the gate then waits for Continue. Only trusted
  // events (our own fill dispatches synthetic ones) and never the panel's own
  // buttons (the panel lives in this document, in its shadow host).
  const holdOnUserInput = (e: Event): void => {
    if (!e.isTrusted || !flowController) return;
    const host = document.getElementById("applypilot-overlay-host");
    if (host && e.composedPath().includes(host)) return;
    flowController.holdAutoAdvance();
  };
  document.addEventListener("pointerdown", holdOnUserInput, true);
  document.addEventListener("keydown", holdOnUserInput, true);
  // From the start, so a box the user clicked into before a fill is still
  // theirs, and one the page autofocused is not (userFocus.ts).
  trackUserFocus(document);

  /** Resume a persisted flow after a real navigation (form-owning frame only,
   *  or the top frame of an entry page, job posting / apply-method chooser). */
  async function maybeResumeFlow(): Promise<void> {
    if (flowController || resumeInFlight) return;
    if (
      recognizedCount(lastFields) === 0 &&
      !(isTopFrame && findApplyEntry(document, lastAdapter))
    ) {
      return; // not the form-owning frame (yet), and nothing to click into
    }
    resumeInFlight = true;
    try {
      const resp = await sendToBackground<FlowStateResponse>({ type: "FLOW_STATE_GET" });
      const st = resp?.state;
      if (!st?.active) return;
      if (Date.now() - st.startedAt > FLOW_TTL_MS) {
        void sendToBackground<SimpleResponse>({ type: "FLOW_STATE_SET", state: null }).catch(() => {});
        return;
      }
      await readFlowSettings();
      if (flowController) return; // an autofill click set it while we awaited FLOW_STATE_GET
      flowAbort = new AbortController(); // a resumed flow is cancellable too
      void setDialogSuppression(true); // a resumed flow may auto-advance (beforeunload)
      flowController = new FlowController(makeFlowDeps());
      void flowController.run(st, null);
    } catch {
      // Background asleep: the flow simply doesn't resume.
    } finally {
      resumeInFlight = false;
    }
  }

  const overlayCallbacks: OverlayCallbacks = {
    onAutofill: async (ids: string[], uploadResumeId?: number | null) => {
      flowResumeId = uploadResumeId ?? null;
      // Suppress blocking page dialogs (alert / beforeunload) for the fill+flow.
      void setDialogSuppression(true);
      // One click = one flow. Replace any prior flow, fill this step now (the
      // panel awaits this first tally), then let the controller advance.
      // Invariant: a Stop during this initial fill wins (skip the controller);
      // a fresh click supersedes a background resume that started mid-fill.
      const gen = flowGeneration;
      flowController?.stop();
      flowAbort?.abort(); // supersede any fill still running from a prior click
      flowAbort = new AbortController();
      const signal = flowAbort.signal;
      // What the panel showed when Autofill was clicked: fields that appear
      // after this (rows the fill itself adds) are filled by default.
      const knownAtClick = new Set(lastFields.map((f) => f.id));
      const [tally] = await Promise.all([fillOnce(ids, signal, knownAtClick), readFlowSettings()]);
      if (gen === flowGeneration && !signal.aborted) {
        flowController?.stop(); // a maybeResumeFlow may have set one mid-fill; this click wins
        flowController = new FlowController(makeFlowDeps());
        void flowController.run(
          { active: true, step: 0, startedAt: Date.now(), lastSignature: "" },
          tally
        );
      }
      return tally;
    },
    /**
     * The user answered the questions autofill couldn't. Write them into the
     * page, and save the ones that have a real profile slot to their Tailrd
     * profile (planAnswerSaves decides, nothing else is persisted anywhere).
     *
     * The page write comes first and its result is what we report, a save that
     * "succeeded" while the form stayed empty would be a lie. The profile save
     * is best-effort: a backend hiccup must not lose the answers the user can
     * already see filled in.
     */
    onAnswerGaps: async (answers) => {
      const targets = answers
        .map((a) => ({ fieldId: a.gap.fieldId, value: a.value.trim() }))
        .filter((t) => t.value);
      const { outcomes } = await fillItems(targets, true);
      const filledIds = new Set(outcomes.filter((o) => o.ok).map((o) => o.fieldId));
      const filled = filledIds.size;

      // Persistence must not outlive a failed write where the failure proves the
      // answer unusable, a profile value is replayed on every future form (see
      // answersWorthRemembering). An ordinary free-text answer that missed is
      // still kept: the write failed, the answer did not. `discarded` goes back
      // to the panel so the banner never claims to have saved what we dropped.
      const keep = answersWorthRemembering(answers, filledIds);
      const discarded = answers.length - keep.length;
      const plan = planAnswerSaves(keep);

      if (Object.keys(plan.profilePatch).length > 0) {
        const resp = await sendToBackground<ProfileResponse>({
          type: "UPDATE_PROFILE",
          update: plan.profilePatch,
        }).catch(() => null);
        if (!resp?.ok) {
          return {
            ok: true,
            filled,
            discarded,
            reason:
              "Filled the form, but couldn't save to your profile. You may be asked these again.",
          };
        }
        lastProfile = resp.profile ?? lastProfile;
      }
      return { ok: true, filled, discarded };
    },
    /**
     * Read the real options of each field's control for the gaps modal.
     * Sequential and budgeted: each harvest opens and closes a dropdown on the
     * page, and doing several at once would leave two listboxes open at the
     * same time, which several ATS treat as a click-away and close both.
     */
    onHarvestGapOptions: async (fieldIds) => {
      const HARVEST_BUDGET_MS = 4000;
      const PER_WIDGET_MS = 600;
      const out: Record<string, string[]> = {};
      const deadline = Date.now() + HARVEST_BUDGET_MS;
      for (const id of fieldIds) {
        if (Date.now() >= deadline) break;
        const el = registry.get(id)?.el;
        if (!el) continue;
        const options = await harvestComboboxOptions(el, { openWaitMs: PER_WIDGET_MS }).catch(
          () => undefined
        );
        if (options && options.length > 0) out[id] = options;
      }
      return out;
    },
    onFlowStop: () => {
      flowGeneration++; // a Stop during an in-flight initial fill must win the race
      flowAbort?.abort(); // interrupt a fill currently writing fields (cancel mid-fill)
      void setDialogSuppression(false); // restore the page's dialogs
      flowController?.stop();
      flowController = null;
      // Stop always clears the persisted state, even when no controller is
      // live yet (a resume mid-await), so the pending resume cannot proceed.
      void sendToBackground<SimpleResponse>({ type: "FLOW_STATE_SET", state: null }).catch(() => {});
    },
    onFlowAdvance: () => {
      // User pressed "Next page": release the flow's ready gate so it advances
      // and auto-fills the next page (Task B). No-op when no flow is parked.
      flowController?.notifyAdvanceRequested();
    },
    onFlowPause: () => {
      // The countdown's Pause: stay on this page until the user presses Continue.
      flowController?.holdAutoAdvance();
    },
    onRescan: () => {
      runScan();
      reportFields();
    },
    onListResumes: async () => {
      const resp = await sendToBackground<ResumesResponse>({ type: "GET_RESUMES" });
      return resp?.ok ? resp.resumes : [];
    },
    onProfileResolved: (profile) => {
      // The overlay resolved the account profile. Remember it and re-scan so
      // every field gets a proposed value; then push the enriched fields back so
      // the overlay can pre-select them and enable the Autofill button. Without
      // this the scanner only ever ran with a null profile (the legacy popup was
      // the only thing that sent SCAN_PAGE), so nothing was ever fillable.
      lastProfile = profile;
      runScan();
      reportFields();
      void maybeResumeFlow();
    },
    onOpenAiModal: (kind: "resume" | "cover") => {
      void (async () => {
        const auth = await sendToBackground<AccessTokenResponse>({ type: "GET_ACCESS_TOKEN" });
        if (!auth?.ok || !auth.token) {
          void sendToBackground<SimpleResponse>({ type: "OPEN_DASHBOARD" }).catch(() => {});
          return;
        }
        let appOrigin: string;
        try {
          appOrigin = new URL(auth.apiBaseUrl ?? "").origin;
        } catch {
          return;
        }
        const jc = await resolveJobContext();
        openAiModal({
          kind,
          appOrigin,
          job: {
            title: jc.jobTitle,
            company: jc.company,
            description: jc.jobDescription,
            url: location.href,
          },
          getToken: async () => auth.token,
          refreshToken: async () => {
            const r = await sendToBackground<AccessTokenResponse>({ type: "GET_ACCESS_TOKEN" });
            return r?.token ?? auth.token;
          },
          onAttach: async (attachKind, file) => {
            const category = attachKind === "resume" ? "resumeUpload" : "coverLetter";
            const field = lastFields.find(
              (f) => f.category === category && f.controlType === "file"
            );
            const control = field ? registry.get(field.id) : undefined;
            if (!control?.el) return;
            await attachOrGuide(control.el, file.dataBase64, file.filename, file.contentType);
          },
          mount: document.body,
        });
      })();
    },
    onUploadResume: async (resumeId: number) => {
      const field = lastFields.find(
        (f) => f.category === "resumeUpload" && f.controlType === "file"
      );
      const control = field ? registry.get(field.id) : undefined;
      if (!control?.el) {
        return { ok: false, reason: "No résumé upload field found on this page." };
      }
      const file = await sendToBackground<ResumeFileResponse>({
        type: "DOWNLOAD_RESUME",
        resumeId,
      });
      if (!file?.ok || !file.dataBase64) {
        return { ok: false, reason: file?.error ?? "Could not download your résumé." };
      }
      return attachOrGuide(control.el, file.dataBase64, file.name, file.contentType);
    },
    onTailorResume: async (opts: TailorResumeOpts) => {
      const resp = await sendToBackground<TailorResumeResponse>({
        type: "TAILOR_RESUME",
        resumeId: opts.resumeId,
        jobContext: extractJobContext(),
        sections: opts.sections,
        addKeywords: opts.addKeywords,
      });
      if (!resp?.ok || !resp.result) {
        return {
          ok: false,
          needsLogin: resp?.needsLogin,
          reason: resp?.error ?? "Couldn't prepare suggestions.",
        };
      }
      return { ok: true, result: resp.result };
    },
    onAttachTailored: async (document: ResumeDoc) => {
      const field = lastFields.find(
        (f) => f.category === "resumeUpload" && f.controlType === "file"
      );
      const control = field ? registry.get(field.id) : undefined;
      if (!control?.el) {
        return { ok: false, reason: "No résumé upload field found on this page." };
      }
      const company = extractJobContext().company;
      const file = await sendToBackground<RenderResumeResponse>({
        type: "RENDER_RESUME",
        document,
        filename: company ? `resume-${company}` : "resume",
      });
      if (!file?.ok || !file.dataBase64) {
        return { ok: false, reason: file?.error ?? "Could not render your résumé." };
      }
      return attachOrGuide(control.el, file.dataBase64, file.name, file.contentType);
    },
    onDownloadTailored: async (document: ResumeDoc) => {
      const company = extractJobContext().company;
      const file = await sendToBackground<RenderResumeResponse>({
        type: "RENDER_RESUME",
        document,
        filename: company ? `resume-${company}` : "resume",
      });
      if (!file?.ok || !file.dataBase64) {
        return { ok: false, reason: file?.error ?? "Could not render your résumé." };
      }
      downloadBase64File(file.dataBase64, file.name, file.contentType);
      return { ok: true };
    },
    onGenerateCoverLetter: async (opts: CoverLetterGenOpts) => {
      const resp = await sendToBackground<GenerateCoverLetterResponse>({
        type: "GENERATE_COVER_LETTER",
        resumeId: opts.resumeId,
        jobContext: extractJobContext(),
        tone: opts.tone,
        baseText: opts.baseText,
      });
      if (!resp?.ok || typeof resp.text !== "string") {
        return {
          ok: false,
          needsLogin: resp?.needsLogin,
          reason: resp?.error ?? "Couldn't gather cover letter ideas.",
        };
      }
      return { ok: true, text: resp.text };
    },
    onInsertCoverLetter: async (text: string) => {
      // Prefer a cover-letter textarea; fall back to a cover-letter file field.
      const textField = lastFields.find(
        (f) => f.category === "coverLetter" && LONG_TEXT.includes(f.controlType)
      );
      if (textField) {
        const control = registry.get(textField.id);
        if (!control) return { ok: false, reason: "Cover-letter field is no longer on the page. Rescan." };
        const res = writeControl(control, text);
        if (!res.written) return { ok: false, reason: res.reason };
        return verifyControl(control, text)
          ? { ok: true }
          : { ok: false, reason: "Text did not stick. Please check the field." };
      }
      const fileField = lastFields.find(
        (f) => f.category === "coverLetter" && f.controlType === "file"
      );
      const fileControl = fileField ? registry.get(fileField.id) : undefined;
      if (fileControl?.el) {
        const company = extractJobContext().company;
        const file = await sendToBackground<RenderCoverLetterResponse>({
          type: "RENDER_COVER_LETTER",
          text,
          filename: company ? `cover-letter-${company}` : "cover-letter",
        });
        if (!file?.ok || !file.dataBase64) {
          return { ok: false, reason: file?.error ?? "Could not render your cover letter." };
        }
        return attachOrGuide(fileControl.el, file.dataBase64, file.name, file.contentType);
      }
      return { ok: false, reason: "No cover-letter field found on this page." };
    },
    onDownloadCoverLetter: async (text: string) => {
      const company = extractJobContext().company;
      const file = await sendToBackground<RenderCoverLetterResponse>({
        type: "RENDER_COVER_LETTER",
        text,
        filename: company ? `cover-letter-${company}` : "cover-letter",
      });
      if (!file?.ok || !file.dataBase64) {
        return { ok: false, reason: file?.error ?? "Could not render your cover letter." };
      }
      downloadBase64File(file.dataBase64, file.name, file.contentType);
      return { ok: true };
    },
    onCopyCoverLetter: async (text: string) => {
      try {
        await navigator.clipboard.writeText(text);
        return { ok: true };
      } catch {
        return { ok: false, reason: "Clipboard blocked. Select the text and copy manually." };
      }
    },
  };

  /** Page-context hint for the mount gate, lets two weak job-flavored fields
   *  count as an application when the URL/title says we're on a job page. */
  function pageContext(): PageContext {
    return { url: location.href, title: document.title };
  }

  function maybeShowOrUpdateOverlay(carryReverted = false): void {
    if (!isTopFrame || adoptedRemote) return;
    const entry = findApplyEntry(document, lastAdapter);
    const ident = extractJobIdentity();
    // Which ATS are we on? The matched adapter's label is the most reliable
    // signal (broad host match); fall back to the path-gated registry for the
    // company portals that have no fill adapter. Either may be null.
    const site = detectSite(location.hostname, location.href, { inIframe: self !== top });
    const siteLabel = lastAdapter?.label ?? site?.label ?? null;
    const state = {
      fields: lastFields,
      tabUrl: location.href,
      applyEntry: entry?.label ?? null,
      company: ident.company,
      jobTitle: ident.jobTitle,
      siteLabel,
      // Only the post-fill push carries this. An ordinary observer rescan must
      // leave the panel's copy alone: sending an empty set from every scan
      // would clear what the fill's re-scan observed before the gap card is
      // even rendered.
      ...(carryReverted ? { reverted: lastRevertedIds } : {}),
    };
    // Mount on a detected job-application form, on a known ATS's apply-entry
    // page (Workday job posting), or on an ATS application page, so the
    // always-on "Autofill" button is available to start the flow even before
    // the posting exposes an apply-entry we recognise.
    //
    // "ATS page" is deliberately NOT a bare adapter match: adapters match by
    // host only (/(^|\.)greenhouse\.io$/ …), so the vendors' own marketing
    // sites popped the panel on every page. The registry IS path-gated
    // (Greenhouse only on /{co}/jobs/{id}), so prefer it, and fall back to the
    // adapter only where the page actually has recognized fields, that keeps
    // adapter hosts missing from the registry working without re-opening the
    // marketing-page hole.
    //
    // Generic recognized fields (name/email/phone…) are NOT evidence on their
    // own: login, checkout and contact forms have those on every site. The
    // toolbar icon still opens the panel on demand anywhere.
    const atsPage = Boolean(site) || (Boolean(lastAdapter) && recognizedCount(lastFields) > 0);
    const shouldMount =
      looksLikeJobApplication(lastFields, pageContext()) ||
      Boolean(entry?.fromAdapter) ||
      atsPage ||
      flowActiveHint;
    if (!overlayShown && shouldMount) {
      overlayShown = true;
      console.log(`[Tailrd overlay] mounting panel (recognized=${recognizedCount(lastFields)} of ${lastFields.length} fields, entry=${entry?.label ?? "none"}, ats=${siteLabel ?? "none"})`);
      showOverlay(state, overlayCallbacks);
    } else if (overlayShown || panelOpenedManually) {
      updateOverlay(state);
    } else {
      console.log(`[Tailrd overlay] NOT mounting: no job-application evidence in TOP frame (${recognizedCount(lastFields)} recognized fields, no ATS match)`);
    }
  }

  // ---- Observer ---------------------------------------------------------------

  /** Start watching for SPA re-renders after the first scan request. */
  function ensureObserver(): void {
    if (observer) return;
    observer = observePage(() => {
      const before = lastFields.length;
      const recognizedBefore = recognizedCount(lastFields);
      runScan();
      // Keep the reconciler pointed at the freshly-scanned controls so its
      // background drift correction tracks surviving fields after re-renders.
      engine?.updateRegistry(registry);
      if (lastFields.length !== before) {
        // Let the toolbar popup know (it refreshes if open).
        const event: FieldsUpdatedEvent = {
          type: "FIELDS_UPDATED",
          url: location.href,
          fieldCount: lastFields.length,
        };
        // Context-safe: after a reload this send would otherwise throw
        // "Extension context invalidated" synchronously on every mutation.
        postToRuntime(event);
      }
      reportFields();
      if (!isTopFrame) announceIfFormHost();
      // A mid-flow navigation can land on an SPA page (Workday) that lazy-renders
      // its fields AFTER the initial profile-resolve resume already ran and bailed
      // on the still-empty page. Retry the resume the instant recognized fields
      // appear, so the next page auto-fills without a manual Autofill click. Gated
      // on the profile being loaded (a fill needs proposed values) and on a real
      // 0→N transition; maybeResumeFlow's own guards handle the no-active-flow case.
      if (lastProfile !== null && recognizedBefore === 0 && recognizedCount(lastFields) > 0) {
        void maybeResumeFlow();
      }
    });
  }

  /**
   * Load the server-side hot-fix classification rules for this host, then
   * re-scan so they take effect. Best-effort, no rules / background asleep just
   * leaves the generic pipeline running.
   */
  async function loadOverrides(): Promise<void> {
    try {
      const resp = await sendToBackground<OverridesResponse>({ type: "GET_OVERRIDES" });
      if (!resp?.ok || resp.rules.length === 0) return;
      setOverrideRules(resp.rules, location.host);
      runScan(); // re-classify with the overrides applied
      engine?.updateRegistry(registry);
      reportFields();
      if (!isTopFrame) announceIfFormHost();
    } catch {
      // No rules or background asleep, generic classification stands.
    }
  }

  /**
   * If this page is a real job posting (has a substantial description), remember
   * it so the application form page, which almost never repeats the description
   * can still give the AI real context for the résumé rewrite / cover letter.
   */
  function captureJobDescription(): void {
    try {
      const ctx = extractJobContext();
      if (ctx.jobDescription && ctx.jobDescription.length >= MIN_CACHEABLE_DESC) {
        const place = detectJobPlace(document);
        void saveLastJobContext({
          jobDescription: ctx.jobDescription,
          jobTitle: ctx.jobTitle,
          company: ctx.company,
          url: location.href,
          country: place.country,
          city: place.city,
        });
      }
    } catch {
      /* best effort */
    }
  }

  /**
   * Job context for the AI modals: this page's context, but falling back to the
   * last posting we saw for the description (application forms rarely have it).
   */
  async function resolveJobContext(): Promise<JobContext> {
    const ctx = extractJobContext();
    if (ctx.jobDescription && ctx.jobDescription.length >= MIN_CACHEABLE_DESC) return ctx;
    const cached = await getLastJobContext();
    if (!cached) return ctx;
    return {
      jobTitle: ctx.jobTitle || cached.jobTitle,
      company: ctx.company || cached.company,
      jobDescription: cached.jobDescription,
    };
  }

  /** If a live flow is persisted for this tab, let the panel mount for it.
   *  Async: re-runs the mount/announce decision once the hint is known. */
  async function probeFlowHint(): Promise<void> {
    try {
      const resp = await sendToBackground<FlowStateResponse>({ type: "FLOW_STATE_GET" });
      const st = resp?.state;
      if (!st?.active || Date.now() - st.startedAt > FLOW_TTL_MS) return;
      flowActiveHint = true;
      if (isTopFrame) maybeShowOrUpdateOverlay();
      else announceIfFormHost();
    } catch {
      // Background asleep: no hint; the panel can still be opened manually.
    }
  }

  function autoInit(): void {
    runScan();
    captureJobDescription();
    ensureObserver();
    void loadOverrides();
    void ensurePageGuesses();
    void probeFlowHint();
    if (isTopFrame) {
      maybeShowOrUpdateOverlay();
      // A mid-flow REAL navigation can land on a field-less entry page (the
      // apply-method chooser). No fields → no panel → no profile resolve, so
      // the usual profile-driven resume never fires; resume here instead.
      // Entry clicks need no profile, and form pages keep the profile-first
      // resume path (fills would run empty without it).
      if (recognizedCount(lastFields) === 0 && findApplyEntry(document, lastAdapter)) {
        void maybeResumeFlow();
      }
    } else {
      announceIfFormHost();
    }
  }

  /**
   * A child frame that owns a real form tells the top frame about it (the panel
   * lives in the top frame; this frame can't reach it directly). Re-announcing
   * on later scans is the retry if the top frame wasn't listening yet.
   */
  function announceIfFormHost(): void {
    if (isTopFrame) return;
    const recognized = recognizedCount(lastFields);
    if (recognized === 0) return;
    // Same evidence bar as the top frame's mount: a child frame holding only
    // generic fields (login / newsletter / checkout iframes) must not make the
    // top frame pop the panel. An ATS-host iframe (embedded Greenhouse board)
    // announces on its adapter match even before job-specific fields render,
    // and a frame that owns a mid-flow form announces on the live flow.
    if (!lastAdapter && !flowActiveHint && !looksLikeJobApplication(lastFields, pageContext()))
      return;
    actingAsRemoteHost = true;
    void chrome.runtime
      .sendMessage({ type: "FORM_HOST_ANNOUNCE", recognized, fields: lastFields })
      .catch(() => {});
  }

  // Career sites (Databricks/Greenhouse, Workday…) lazily mount the real form
  // after the page settles, the consent banner, or on scroll. When the panel is
  // opened and nothing fillable is visible yet, briefly re-scan so a form that
  // mounts a moment later is still detected. Bounded + stops as soon as a
  // recognized field appears, so it never polls indefinitely.
  let lateMountTimer: ReturnType<typeof setTimeout> | null = null;
  function watchForLateMount(attemptsLeft = 12): void {
    if (lateMountTimer) clearTimeout(lateMountTimer);
    if (attemptsLeft <= 0) {
      lateMountTimer = null;
      return;
    }
    lateMountTimer = setTimeout(() => {
      lateMountTimer = null;
      runScan();
      engine?.updateRegistry(registry);
      reportFields();
      if (!isTopFrame) announceIfFormHost();
      if (recognizedCount(lastFields) === 0) watchForLateMount(attemptsLeft - 1);
    }, 1000);
  }

  // ---- Popup-driven messaging ------------------------------------------------

  chrome.runtime.onMessage.addListener(
    (message: ContentRequest, _sender, sendResponse): boolean => {
      switch (message.type) {
        case "PING": {
          const response: PingResponse = { ok: true, frameToken: FRAME_TOKEN };
          sendResponse(response);
          return false;
        }

        case "JOB_PLACE_GET": {
          // An embedded form in this tab asks where the job is (adoptTopFramePlace).
          if (!isTopFrame) return false;
          refreshResolveContext();
          const c = getResolveContext();
          const response: JobPlaceResponse = { ok: true, country: c.jobCountry, city: c.jobCity ?? null, company: c.company };
          sendResponse(response);
          return false;
        }

        case "TOGGLE_PANEL": {
          if (isTopFrame && adoptedRemote && remoteCallbacks) {
            // The form lives in a child frame, toggle the adopted panel as-is,
            // never a local re-scan that would show the empty top-frame DOM.
            toggleOverlay({ fields: remoteFields, tabUrl: location.href }, remoteCallbacks);
          } else if (isTopFrame) {
            // Re-scan on open so a lazily-/late-mounted form (common on SPA
            // career sites, where the real form mounts after the consent
            // banner) is reflected immediately, and keep watching for mounts
            // that happen while the panel is open.
            runScan();
            ensureObserver();
            const state = {
              fields: lastFields,
              tabUrl: location.href,
              applyEntry: findApplyEntry(document, lastAdapter)?.label ?? null,
            };
            toggleOverlay(state, overlayCallbacks);
            // Keep pushing scan updates to this user-opened panel even though
            // it never auto-mounted (see panelOpenedManually).
            panelOpenedManually = true;
            // Nothing fillable yet? Watch briefly for a lazy-mounted form.
            if (recognizedCount(lastFields) === 0) watchForLateMount();
          }
          sendResponse({ ok: true });
          return false;
        }

        case "SCAN_PAGE": {
          lastProfile = message.profile;
          lastFillEEO = message.fillEEO;
          const response = runScan();
          ensureObserver();
          maybeShowOrUpdateOverlay();

          if (response.fields.length > 0) {
            sendResponse(response); // we have the form, answer first
          } else if (isTopFrame) {
            // Empty top frame: give child frames 400ms to claim the scan.
            setTimeout(() => sendResponse(response), 400);
          } else {
            // Empty child frame: answer last, only as a fallback.
            setTimeout(() => sendResponse(response), 900);
          }
          return true; // keep the channel open for the delayed response
        }

        case "FILL_FIELDS": {
          const mine = message.instructions.filter((i) =>
            i.fieldId.startsWith(`${FRAME_TOKEN}-`)
          );
          if (mine.length > 0) {
            void getEngine()
              .run(mine, registry)
              .then((reports) => {
                const response: FillResponse = {
                  ok: true,
                  outcomes: reports.map(reportToOutcome),
                };
                sendResponse(response);
              });
            return true; // engine resolves after the stability window
          }
          if (isTopFrame) {
            // Fallback so the popup always gets *some* answer if the owning
            // frame disappeared (e.g. iframe navigated away). The owning frame
            // now answers only after its reconciliation settles (up to a few
            // ~800ms cycles), so this must wait long enough not to beat a real
            // owner whose form lives in a child iframe.
            const response: FillResponse = {
              ok: false,
              error: "The form's frame is gone. Rescan the page",
              outcomes: [],
            };
            setTimeout(() => sendResponse(response), 3000);
            return true;
          }
          return false; // not ours, stay silent
        }

        case "FORM_OP": {
          // This frame owns the form; run the requested overlay op locally and
          // return its result to the top-frame panel (via the background relay).
          void dispatchFormOp(overlayCallbacks, message.op, message.args).then(sendResponse);
          return true; // async
        }

        case "REMOTE_FORM_AVAILABLE": {
          // A child frame owns a form. Adopt it only if WE have no form of our
          // own and haven't already mounted a panel for it.
          if (
            isTopFrame &&
            !overlayShown &&
            shouldAdoptRemoteHost(recognizedCount(lastFields), message.recognized)
          ) {
            const frameId = message.frameId;
            const send = (op: FormOpName, args: unknown[]): Promise<FormOpResult> =>
              sendToRuntime<FormOpResult>({ type: "RELAY_FORM_OP", frameId, op, args }) as Promise<FormOpResult>;
            remoteFields = message.fields;
            remoteCallbacks = makeProxyCallbacks(send);
            // The AI modal opens an iframe overlay in THIS (panel) frame, so it
            // must run locally rather than being proxied to the adopted subframe.
            remoteCallbacks.onOpenAiModal = overlayCallbacks.onOpenAiModal;
            overlayShown = true;
            adoptedRemote = true;
            console.log(`[Tailrd overlay] adopting form in frame ${frameId} (${message.recognized} recognized fields)`);
            showOverlay({ fields: remoteFields, tabUrl: location.href }, remoteCallbacks);
          }
          sendResponse({ ok: true });
          return false;
        }

        case "REMOTE_FIELDS_UPDATED": {
          // The child host re-scanned (profile / rescan / mutation), refresh.
          if (isTopFrame && adoptedRemote) {
            remoteFields = message.fields;
            updateOverlay({
              fields: remoteFields,
              tabUrl: location.href,
              // Only the child's post-fill push carries a verdict; an ordinary
              // rescan must not erase the one already on the panel.
              ...(message.reverted ? { reverted: new Set(message.reverted) } : {}),
            });
          }
          sendResponse({ ok: true });
          return false;
        }

        case "REMOTE_FLOW_PROGRESS": {
          // The child host's flow beat, render it on the adopted panel.
          if (isTopFrame && adoptedRemote) updateFlowProgress(message.progress);
          sendResponse({ ok: true });
          return false;
        }

        default:
          return false;
      }
    }
  );

  // Kick off autonomous detection after the initial layout settles.
  if (document.readyState === "complete" || document.readyState === "interactive") {
    autoInit();
  } else {
    window.addEventListener("DOMContentLoaded", autoInit, { once: true });
  }
}
