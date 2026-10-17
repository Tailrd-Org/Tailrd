/**
 * Multi-page autofill flow: fill → advance → fill → … → done/paused/stopped.
 *
 * Pure orchestration over injected deps, no chrome.*, no direct DOM writes,
 * so the whole machine unit-tests with scripted fakes. contentScript provides
 * the real deps (fillOnce, scanner snapshots, advance discovery, background
 * state persistence) and the overlay renders the progress beats.
 *
 * One Autofill click carries the whole application: a page that fills
 * cleanly counts down on the panel's bottom button (AUTO_ADVANCE_MS) and then
 * turns itself, the account wall included. The user can press the button to
 * go at once, or hold the page (Pause, or typing/clicking in it while the
 * countdown runs). A page with a required field still empty waits for the user.
 *
 * Invariants:
 *  - NEVER clicks a terminal (submit-like) button, finishes "done" instead.
 *  - Persists FlowState BEFORE clicking advance, so a real navigation (content
 *    script death) resumes on the next page via the session flag.
 *  - Never clicks an advance found on a page that has since been replaced: a
 *    page the user turned themselves is filled, not skipped.
 *  - Every pause auto-resumes when its condition clears (polled).
 *  - Runaway guards: MAX_STEPS, FLOW_TTL_MS, and a same-signature loop check.
 */
import type {
  DetectedField,
  FlowPauseReason,
  FlowPhase,
  FlowProgress,
  FlowState,
} from "../shared/types";
import type { AdvanceButton } from "./advance";

/** A Workday application is about ten steps (posting, chooser, account, five
 *  or six form pages, review); this is the runaway guard, not a budget. */
export const MAX_STEPS = 16;
/** Bounds UNATTENDED running. A press on the panel's button is fresh intent
 *  and restarts it, so a user who reviews a held page for a while is not told
 *  the flow timed out when they press Continue. */
export const FLOW_TTL_MS = 10 * 60 * 1000;
/** How long a cleanly filled page counts down on the panel's bottom button
 *  before the flow turns it by itself: long enough to see the page and press
 *  Pause, short enough that one Autofill click reaches the Review page. */
export const AUTO_ADVANCE_MS = 2000;
/** How often a parked gate looks for a press, the countdown's end, or the
 *  page having been turned under it. */
const GATE_POLL_MS = 250;
const PAUSE_POLL_MS = 2000;
const ADVANCE_POLL_MS = 500;
const ADVANCE_WAIT_MS = 8000;
/** After an entry click opens a page with no form of its own, how long a
 *  child frame has to claim one (its content script scans, then announces). */
const REMOTE_FORM_WAIT_MS = 6000;
/** How long a filled page's disabled advance gets to enable itself. */
const ADVANCE_ENABLE_WAIT_MS = 3000;
/** How many times to auto-attach a résumé upload field that lazy-renders after
 *  the fill before falling back to waiting for a manual attach. */
const RESUME_ATTACH_TRIES = 6;

/** Pauses a user CAN clear by pressing the panel's advance button. They have
 *  fixed the page (or judged it fine) and want the flow to try again. Captcha,
 *  verification and resume-upload are absent on purpose: the click cannot
 *  clear them, so offering a button that does nothing is worse than none.
 *  Kept in step with showsAdvanceGate() in overlay.ts, which decides whether
 *  the user is offered that button at all. */
export const USER_CLEARABLE_PAUSES: ReadonlySet<FlowPauseReason> = new Set<FlowPauseReason>([
  "validation",
  "unfilled-required",
  "account",
]);

export interface StepTally {
  ok: number;
  fail: number;
  total: number;
}

export interface FlowSnapshot {
  fields: DetectedField[];
  scopeEl: HTMLElement | null;
  /** Page URL at snapshot time, the change signal on field-less entry pages. */
  url: string;
  /** Apply-entry button (job posting / apply-method chooser), when one exists. */
  entry: { el: HTMLElement; label: string } | null;
  /** True while a signup/login account wall is on the page, lets the flow
   *  hand a stuck wall back to the user instead of stopping the whole flow. */
  accountWall: boolean;
  /** The page's form belongs to a child frame (the panel adopted it), so the
   *  flow continues there: iCIMS opens its login and form inside the
   *  posting's own frame. Absent = false. */
  remoteForm?: boolean;
}

export interface FlowDeps {
  /** One full fill pass (fillOnce). null ids → default selection this step.
   *  `afterEntry`: the page was just opened by an apply-entry click, where a
   *  form still loading hidden is worth waiting for (hiddenForm.ts). */
  fillStep(ids: string[] | null, ctx?: { afterEntry?: boolean }): Promise<StepTally>;
  snapshot(): FlowSnapshot;
  /** Force a fresh scan (updates what snapshot() returns). */
  rescan(): void;
  findAdvance(scope: HTMLElement, extraAdvance?: RegExp): AdvanceButton | null;
  /** True while the scope holds an advance/submit button the page keeps
   *  disabled for now (it enables it once its own checks pass). Absent = false. */
  advancePending?(scope: HTMLElement, extraAdvance?: RegExp): boolean;
  clickAdvance(el: HTMLElement): void;
  /** The flow reached the terminal (submit) button, hand it over so the caller
   *  can bind submit tracking. NEVER clicked by the controller. */
  onTerminal?(el: HTMLElement): void;
  /** On a page with no fields, the SITE ADAPTER's own footer button when it is
   *  the submit (Workday's Review), else null. Never a generic search: a
   *  field-less page's other buttons are not the application's. */
  findPageTerminal?(): HTMLElement | null;
  /** Account-wall handling (Phase 4); {} when no wall. `wall` reports the kind
   *  so progress beats can say "creating account…" / "signing in…". */
  accountStep(snap: FlowSnapshot): Promise<{ extraAdvance?: RegExp; wall?: "signup" | "login" }>;
  /** First blocking condition, or null when clear (captcha/validation/…). */
  pauseReason(snap: FlowSnapshot): Promise<FlowPauseReason | null>;
  /** True when a required résumé field needs a file. */
  needsResume(snap: FlowSnapshot): boolean;
  /** True when a required field on this page is still empty (pause on issues). */
  hasUnfilledRequired(snap: FlowSnapshot): boolean;
  /** Try to attach the user's résumé; false → pause until the user does. */
  attachResume(): Promise<boolean>;
  setState(state: FlowState | null): Promise<void>;
  onProgress(p: FlowProgress): void;
  /** Best-effort log of why the page didn't advance (visible validation errors,
   *  empty required fields), surfaces the real blocker on live ATS. No-op in tests. */
  diagnoseStuck?(): void;
  /**
   * Re-read this page and record how it differs from what was written, just
   * before it goes away.
   *
   * A page turn is the last moment this page is observable. Anything a
   * framework reverted after the fill, on blur, on its own validation, on a
   * re-render some later field triggered, is invisible from the next page, and
   * per-write verification cannot see it either, because it happened after the
   * write was verified. Called before every advance click and once when the
   * flow finishes; failures are the caller's to swallow.
   */
  auditPageState?(): Promise<void>;
  /** False parks every filled page at the manual gate instead of counting
   *  down (a setting; the e2e harness turns it off to read each page).
   *  Absent = true. */
  autoContinue?(): boolean;
  sleep(ms: number): Promise<void>;
  now(): number;
}

/** Order-independent hash of the scanned field set, step-change detection. */
export function fieldSignature(fields: DetectedField[]): string {
  const s = fields
    .map((f) => `${f.category}|${f.label}|${f.controlType}`)
    .sort()
    .join("\n");
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return `${fields.length}:${(h >>> 0).toString(16)}`;
}

/**
 * The "did the page change?" signal for one step. Field-less pages (job
 * posting, apply-method chooser) all hash to the same fieldSignature, so there
 * the URL + entry-button label stand in, an SPA that swaps "Apply" for
 * "Apply Manually" without navigating still reads as a new page.
 */
export function stepSignature(snap: FlowSnapshot): string {
  if (snap.fields.length > 0) return fieldSignature(snap.fields);
  return `page:${snap.url}|${snap.entry?.label ?? ""}`;
}

/**
 * The page under a parked gate is no longer the page that was filled: the user
 * turned it with the site's own button. Most of the filled page's questions are
 * gone and others stand in their place.
 *
 * A page that merely GREW is not a turn: a conditional question appearing, an
 * "Add another" row, every field it had is still there. A page mid-render that
 * shows no fields yet is not one either (Workday swaps in a skeleton first).
 */
export function pageTurned(filled: FlowSnapshot, now: FlowSnapshot): boolean {
  if (filled.fields.length === 0 || now.fields.length === 0) return false;
  const key = (f: DetectedField): string => `${f.label}|${f.controlType}`;
  const present = new Set(now.fields.map(key));
  const had = new Set(filled.fields.map(key));
  let kept = 0;
  for (const k of had) if (present.has(k)) kept++;
  return kept / had.size < 0.5;
}

export class FlowController {
  private stopRequested = false;
  private step = 0;
  private startedAt = 0;
  private lastTally = { ok: 0, fail: 0 };
  /** Set by notifyAdvanceRequested(), polled by every wait that a press can
   *  end: the page gate, the account-wall park, a user-clearable pause. */
  private advanceRequested = false;
  /** The page about to be filled was opened by an apply-entry click. */
  private openedFromEntry = false;
  /** A countdown is running on the current page's gate. */
  private counting = false;
  /** The user held the current page (Pause, or input in the page while it
   *  counted down): its gate now waits for a press. Cleared per page. */
  private held = false;
  /** The current gate's label, re-sent when a hold turns it manual. */
  private gateLabel: string | undefined;

  constructor(private deps: FlowDeps) {}

  /** User pressed Stop (or a new flow replaces this one). Idempotent. A
   *  parked gate notices within one poll and run() unwinds as stopped. */
  stop(): void {
    if (this.stopRequested) return;
    this.stopRequested = true;
    this.counting = false;
    void this.deps.setState(null);
  }

  /**
   * User pressed the panel's bottom button: turn the page now (cutting a
   * countdown short), or release a pause they own. A press that lands while
   * no gate is open is dropped when the next page starts.
   */
  notifyAdvanceRequested(): void {
    this.advanceRequested = true;
  }

  /**
   * The user wants to stay on this page: stop the countdown and wait for a
   * press instead (the panel's Pause, or the user typing or clicking in the
   * page while it counts down). Only this page: the next page that fills
   * cleanly counts down again. No-op when no countdown is running.
   */
  holdAutoAdvance(): void {
    if (!this.counting || this.held || this.stopRequested) return;
    this.held = true;
    this.counting = false;
    console.log("[Tailrd flow] countdown held by the user; waiting for Continue");
    this.emit("ready", { nextLabel: this.gateLabel });
  }

  /**
   * Run from `initial` (fresh click: step 0; navigation resume: persisted
   * state). `firstTally` carries the fill the panel already awaited, so the
   * first step is not filled twice.
   */
  async run(initial: FlowState, firstTally: StepTally | null): Promise<void> {
    this.step = initial.step;
    this.startedAt = initial.startedAt || this.deps.now();
    const state: FlowState = { ...initial, startedAt: this.startedAt };
    await this.deps.setState(state);
    let pending = firstTally;

    while (!this.stopRequested) {
      if (this.step >= MAX_STEPS) return this.finish("stopped", "Step limit reached. Review the page");
      if (this.expired()) return this.finish("stopped", "Flow timed out");
      // A new step invalidates any press that arrived while no gate was open
      // (a double-click on the last page), so it can never auto-turn this one.
      this.advanceRequested = false;

      const account = await this.deps.accountStep(this.deps.snapshot());
      const wallDetail =
        account.wall === "signup" ? "creating account…" : account.wall === "login" ? "signing in…" : undefined;

      const tally = pending ?? (await this.deps.fillStep(null, { afterEntry: this.openedFromEntry }));
      pending = null;
      const afterEntry = this.openedFromEntry;
      this.openedFromEntry = false;
      // Cumulative across steps: the final "done" beat reports the whole flow.
      this.lastTally = { ok: this.lastTally.ok + tally.ok, fail: this.lastTally.fail + tally.fail };
      this.emit("filling", { detail: wallDetail });

      if (this.deps.needsResume(this.deps.snapshot()) && !(await this.deps.attachResume())) {
        // attachResume failed (no résumé on file), wait for a manual attach.
      }
      if (!(await this.waitWhileBlocked())) return this.finishStopped();

      const snap = this.deps.snapshot();
      const recognized = snap.fields.filter((f) => f.category !== "unknown").length;
      let adv = snap.scopeEl ? this.deps.findAdvance(snap.scopeEl, account.extraAdvance) : null;
      if (!adv && snap.scopeEl && this.deps.advancePending?.(snap.scopeEl, account.extraAdvance)) {
        adv = await this.waitForAdvanceEnabled(snap.scopeEl, account.extraAdvance);
      }
      // An <input type="submit"> shows its value (iCIMS's "Next").
      const advText = adv
        ? (adv.el.getAttribute("aria-label") || adv.el.textContent?.trim() || (adv.el as HTMLInputElement).value || "")
            .trim()
            .slice(0, 40)
        : "";
      console.log(`[Tailrd flow] step ${this.step}: ${snap.fields.length} fields, advance=${adv ? `${adv.kind} "${advText}"` : "NONE"}`);
      if (!adv) {
        // No advance inside a form scope. On a page with no recognized fields,
        // an apply-entry button (job posting "Apply", chooser "Apply Manually")
        // is the way forward, click it and treat the transition as a step.
        if (recognized === 0 && snap.entry) {
          console.log(`[Tailrd flow] clicking apply entry "${snap.entry.label}"…`);
          if (!(await this.advanceStep(snap, snap.entry.el, `opening "${snap.entry.label}"…`))) {
            if (this.stopRequested) return this.finishStopped();
            return this.finish("stopped", "Couldn't open the application from this page");
          }
          this.openedFromEntry = true;
          continue;
        }
        // The form is a child frame's (iCIMS: the login and the application
        // open inside the posting's own frame). That frame's flow resumes from
        // the step persisted before the entry click, so this one ends without
        // a beat and without clearing it. Right after an entry click the frame
        // may not have claimed its form yet: give it a moment.
        if (recognized === 0) {
          const where = snap.remoteForm ? "remote" : afterEntry ? await this.waitForRemoteForm() : "none";
          if (where === "remote") {
            console.log("[Tailrd flow] the form is in a child frame; the flow continues there");
            return;
          }
          if (where === "local") continue;
        }
        // A field-less LAST page: Workday's Review lists the answers and offers
        // only Submit, so there is no form scope to search. Its own footer, when
        // that is the submit, ends the flow there: bound for submit tracking
        // (the application reaches the dashboard), never clicked.
        const terminal = recognized === 0 ? (this.deps.findPageTerminal?.() ?? null) : null;
        if (terminal) {
          this.deps.onTerminal?.(terminal);
          return this.finish("done", "Ready to review and submit");
        }
        if (!snap.scopeEl && recognized === 0) {
          return this.finish("stopped", "No application form found on this page");
        }
        console.log("[Tailrd flow] no advance button, finishing done");
        return this.finish("done");
      }
      if (adv.kind === "terminal") {
        // Reached the real submit button, hand it to the caller for submit
        // tracking, then finish. The controller itself never clicks it.
        this.deps.onTerminal?.(adv.el);
        return this.finish("done", "Ready to review and submit");
      }

      // The page is filled. One Autofill click carries the whole application,
      // so a clean page counts down on the panel's bottom button (labelled
      // like the real one: Continue / Create Account) and turns itself; the
      // account wall too, with the credentials the user saved for exactly
      // this. The user can press the button to go now, or hold the page. A
      // page with a required field still empty waits for the user instead,
      // as a "paused" beat so the panel can say why (the site's own
      // validation would reject the turn anyway).
      const label = advText || undefined;
      // Only a press made at this gate counts: drop one left over from the
      // fill, THEN announce, since onProgress runs synchronously and a press
      // made the instant the gate appears must be kept.
      this.advanceRequested = false;
      const unfilled = this.deps.hasUnfilledRequired(snap);
      const auto = !unfilled && this.deps.autoContinue?.() !== false;
      if (unfilled) {
        console.log("[Tailrd flow] parked: required field(s) still empty; press the advance button to continue anyway");
        this.emit("paused", { pauseReason: "unfilled-required", nextLabel: label });
      } else if (!auto) {
        console.log(`[Tailrd flow] parked at ready, press "${advText || "Next page"}" to advance`);
        this.emit("ready", { nextLabel: label });
      } else {
        console.log(`[Tailrd flow] page filled; "${advText || "Next page"}" in ${AUTO_ADVANCE_MS} ms unless held`);
        this.held = false;
        this.gateLabel = label;
        this.counting = true;
        this.emit("ready", { nextLabel: label, autoAdvanceMs: AUTO_ADVANCE_MS });
      }
      const gate = await this.waitAtGate(snap, auto ? AUTO_ADVANCE_MS : null);
      this.counting = false;
      if (gate === "stop") return this.finishStopped();
      if (gate === "moved") {
        await this.followTurnedPage(snap);
        continue;
      }
      // A blocker (e.g. a captcha) may have re-appeared while the flow waited, so
      // re-check before clicking advance.
      if (!(await this.waitWhileBlocked())) return this.finishStopped();
      // The button found above belongs to the page that was filled. If the user
      // turned the page meanwhile, a site that reuses its footer button (Workday)
      // would have it turn the NEW page, unfilled. Fill that page instead.
      if (pageTurned(snap, this.deps.snapshot())) {
        await this.followTurnedPage(snap);
        continue;
      }

      console.log(`[Tailrd flow] clicking advance "${advText}"…`);
      if (!(await this.advanceStep(snap, adv.el, wallDetail))) {
        if (this.stopRequested) return this.finishStopped();
        this.deps.diagnoseStuck?.(); // surface the real blocker (validation / empty required)
        // An account wall that didn't advance means auto-signup/sign-in
        // couldn't complete on its own, a password the site rejected, an
        // unmet requirement, an email-verification/2FA step, a captcha inside
        // the account form. Hand it to the user and resume when they clear it,
        // instead of killing the whole flow.
        if (account.wall) {
          console.log("[Tailrd flow] account wall didn't advance, pausing for the user to finish");
          // Drop any stray click from before this pause, THEN announce it,
          // onProgress runs synchronously, so a user pressing Continue the
          // instant the gate appears must be honoured, not cleared by the
          // wait we are about to enter.
          this.advanceRequested = false;
          this.emit("paused", { pauseReason: "account" });
          if (!(await this.waitForWallCleared())) return this.finishStopped();
          this.step -= 1; // re-attempt the step now that the wall is gone
          continue;
        }
        // Click rejected (validation) or this page genuinely can't advance.
        // NB: this pre-check consumes one pauseReason() poll, so emit the
        // pause beat here, waitWhileBlocked may find the reason already clear.
        const reason = await this.deps.pauseReason(this.deps.snapshot());
        console.log(`[Tailrd flow] couldn't advance; pauseReason=${reason ?? "none"}`);
        if (reason === "validation") {
          this.emit("paused", { pauseReason: "validation" });
          if (!(await this.waitWhileBlocked())) return this.finishStopped();
          this.step -= 1; // retry the same page without burning a step
          continue;
        }
        return this.finish("stopped", "Couldn't advance past this page");
      }
    }
    return this.finishStopped();
  }

  // -------------------------------------------------------------------------

  /**
   * One page transition: persist the NEXT step's state (before the click, so a
   * real navigation resumes there), emit the advancing beat, click, and wait
   * for the page to change. False → the page never changed (or we stopped).
   */
  private async advanceStep(snap: FlowSnapshot, el: HTMLElement, detail?: string): Promise<boolean> {
    // Last look at this page before it is replaced. See FlowDeps.auditPageState.
    await this.deps.auditPageState?.().catch(() => {});
    const before = stepSignature(snap);
    const state: FlowState = { active: true, step: this.step + 1, startedAt: this.startedAt, lastSignature: before };
    this.step = state.step;
    await this.deps.setState(state); // BEFORE the click, survives navigation
    this.emit("advancing", { detail });
    this.deps.clickAdvance(el);
    const changed = await this.waitForChange(before);
    console.log(`[Tailrd flow] page changed after advance = ${changed}`);
    return changed;
  }

  private expired(): boolean {
    return this.deps.now() - this.startedAt > FLOW_TTL_MS;
  }

  private emit(phase: FlowPhase, extra: Partial<FlowProgress> = {}): void {
    this.deps.onProgress({
      phase,
      step: this.step,
      filledOk: this.lastTally.ok,
      filledFail: this.lastTally.fail,
      ...extra,
    });
  }

  private async finish(phase: "done" | "stopped", detail?: string): Promise<void> {
    // The final page never gets an advance click, so this is its only audit,
    // and it is the page the user is about to submit.
    await this.deps.auditPageState?.().catch(() => {});
    await this.deps.setState(null);
    this.emit(phase, { detail });
  }

  private finishStopped(): Promise<void> {
    return this.finish("stopped", "Autofill flow stopped");
  }

  /**
   * Park at a filled page's gate until the page should turn:
   *  - "advance": the user pressed the panel's button, or the countdown
   *    (`autoMs`, null for a manual gate) ran out without a hold;
   *  - "moved": the page was turned under the gate (the site's own button), so
   *    nothing must be clicked: the new page is filled instead;
   *  - "stop": stop().
   * A turn is believed only when two polls in a row see it, so a re-render
   * that briefly shows a different field set cannot fake one.
   */
  private async waitAtGate(filled: FlowSnapshot, autoMs: number | null): Promise<"advance" | "moved" | "stop"> {
    const since = this.deps.now();
    let turnedPolls = 0;
    for (;;) {
      if (this.stopRequested) return "stop";
      if (this.advanceRequested) {
        this.advanceRequested = false;
        this.startedAt = this.deps.now(); // a press is fresh intent: restart the TTL
        return "advance";
      }
      if (autoMs !== null && !this.held && this.deps.now() - since >= autoMs) return "advance";
      turnedPolls = pageTurned(filled, this.deps.snapshot()) ? turnedPolls + 1 : 0;
      if (turnedPolls >= 2) return "moved";
      await this.deps.sleep(GATE_POLL_MS);
    }
  }

  /** The user turned the page themselves while it waited at its gate: count
   *  the step and persist it (a resume after a reload starts here), then let
   *  the loop fill the new page. Nothing is clicked. */
  private async followTurnedPage(filled: FlowSnapshot): Promise<void> {
    console.log("[Tailrd flow] the page was turned under the gate; filling the new page");
    this.step += 1;
    this.startedAt = this.deps.now(); // the user acted: restart the TTL
    await this.deps.setState({
      active: true,
      step: this.step,
      startedAt: this.startedAt,
      lastSignature: stepSignature(filled),
    });
  }

  /** Park while an account wall the flow couldn't auto-pass is still on screen.
   *  Resolves true when the wall clears on its own (the user signed in, or the
   *  page moved on) OR when the user presses the panel's advance button; false
   *  on stop/expiry. Rescans each poll so a real navigation (or an SPA step
   *  change) is noticed.
   *
   *  The manual release matters: an account wall the flow could not pass (the
   *  site rejected the password, an extra field is required) leaves the user
   *  looking at a filled form with no way to tell the flow to try again. Auto-
   *  clearing alone cannot cover that, the wall is still on screen. */
  private async waitForWallCleared(): Promise<boolean> {
    for (;;) {
      if (this.stopRequested) return false;
      if (this.expired()) {
        await this.finish("stopped", "Flow timed out");
        return false;
      }
      await this.deps.sleep(PAUSE_POLL_MS);
      if (this.advanceRequested) {
        this.advanceRequested = false;
        return true; // the user says this wall is dealt with, re-attempt
      }
      this.deps.rescan();
      const snap = this.deps.snapshot();
      if (!snap.accountWall) return true;
    }
  }

  /** Poll pauseReason until clear. False → stopped/expired. */
  private async waitWhileBlocked(): Promise<boolean> {
    let current: FlowPauseReason | null = null;
    let resumeTries = 0;
    for (;;) {
      if (this.stopRequested) return false;
      if (this.expired()) {
        await this.finish("stopped", "Flow timed out");
        return false;
      }
      const reason = await this.deps.pauseReason(this.deps.snapshot());
      if (!reason) return true;
      // The user pressed Continue: on a pause they own, that means "I have
      // dealt with this, go". Checked before the emit so a press that lands
      // during the synchronous onProgress is not dropped.
      if (this.advanceRequested && USER_CLEARABLE_PAUSES.has(reason)) {
        this.advanceRequested = false;
        return true;
      }
      // A résumé upload field often lazy-renders after the page fills (Workday and
      // other SPAs), so the one attach attempt at fill time misses it. Auto-attach
      // it here as it appears, the user should never have to click attach. Bounded
      // so a page whose résumé simply has no stored file doesn't hammer the backend;
      // once the tries are spent we fall back to the manual-attach pause below.
      if (reason === "resume-upload" && resumeTries < RESUME_ATTACH_TRIES) {
        resumeTries++;
        if (await this.deps.attachResume()) continue; // injected, re-poll; likely clears now
      }
      if (reason !== current) {
        current = reason;
        this.emit("paused", { pauseReason: reason });
      }
      await this.deps.sleep(PAUSE_POLL_MS);
    }
  }

  /** The filled page's advance is still disabled: give the page a moment to
   *  enable it (its own checks run after the fill's events), then look again. */
  private async waitForAdvanceEnabled(scope: HTMLElement, extraAdvance?: RegExp): Promise<AdvanceButton | null> {
    console.log("[Tailrd flow] the page's advance is still disabled; waiting for it");
    for (let waited = 0; waited < ADVANCE_ENABLE_WAIT_MS; waited += GATE_POLL_MS) {
      if (this.stopRequested) return null;
      await this.deps.sleep(GATE_POLL_MS);
      const adv = this.deps.findAdvance(scope, extraAdvance);
      if (adv) return adv;
      if (!this.deps.advancePending?.(scope, extraAdvance)) break;
    }
    console.log("[Tailrd flow] the advance never enabled");
    return null;
  }

  /** A page an entry click opened shows no form of its own: poll until a
   *  child frame claims one ("remote"), the page grows its own fields or an
   *  entry ("local", fill it), or the wait runs out ("none"). */
  private async waitForRemoteForm(): Promise<"remote" | "local" | "none"> {
    for (let waited = 0; waited < REMOTE_FORM_WAIT_MS; waited += ADVANCE_POLL_MS) {
      if (this.stopRequested) return "none";
      await this.deps.sleep(ADVANCE_POLL_MS);
      this.deps.rescan();
      const snap = this.deps.snapshot();
      if (snap.remoteForm) return "remote";
      if (snap.entry || snap.fields.some((f) => f.category !== "unknown")) return "local";
    }
    return "none";
  }

  /** After an advance click: rescan until the page (fields, or URL/entry on
   *  field-less pages) changes. */
  private async waitForChange(before: string): Promise<boolean> {
    for (let waited = 0; waited < ADVANCE_WAIT_MS; waited += ADVANCE_POLL_MS) {
      if (this.stopRequested) return false;
      await this.deps.sleep(ADVANCE_POLL_MS);
      this.deps.rescan();
      if (stepSignature(this.deps.snapshot()) !== before) return true;
    }
    return false;
  }
}
