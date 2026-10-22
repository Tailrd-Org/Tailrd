// chrome-extension/src/content/adapters/types.ts
/**
 * Per-site adapter contract. A matched adapter layers optional overrides on the
 * generic pipeline; every hook is advisory (undefined = keep generic behavior).
 */
import type { Classification } from "../fieldMatcher";
import type { FieldSignals } from "../domUtils";
import type { RuntimeControl } from "../formScanner";
import type { ControlType, FieldCategory, ResolveControl, UserApplicationProfile } from "../../shared/types";

export interface FieldContext {
  el: HTMLElement;
  signals: FieldSignals;
  controlType: ControlType;
}

export interface AnswerContext {
  category: FieldCategory;
  profile: UserApplicationProfile; // only supplied when a profile is loaded
  control: ResolveControl;
  fillEEO: boolean;
  el: HTMLElement;
}

export interface FillContext {
  control: RuntimeControl;
  value: string;
  el: HTMLElement;
}

export interface AdapterFillResult {
  filled: boolean;
  reason?: string;
}

export interface SiteAdapter {
  id: string;
  /** Human label for the panel ("Workday", "iCIMS"); falls back to id. */
  label?: string;
  /** Detection: pure, host/url only, no DOM. */
  match(host: string, url: string): boolean;
  /** Correct a field's category; undefined keeps the generic Classification. */
  classify?(ctx: FieldContext, generic: Classification): Classification | undefined;
  /** Site-specific value; undefined = generic, string|null = use verbatim. */
  resolveAnswer?(ctx: AnswerContext): string | null | undefined;
  /** undefined (sync) declines → generic fill; a Promise claims + fills the field. */
  fillOperation?(ctx: FillContext): Promise<AdapterFillResult> | undefined;
  /** What a control holds when the page keeps it outside the control (an
   *  input it fills only at submit); undefined reads the control itself. */
  currentValue?(el: HTMLElement): string | undefined;
  /** The step's advance (Next/Continue) button, when the site needs an exact
   *  selector. The generic text-based discovery runs when undefined. The
   *  returned button is still terminal-checked, a Submit is never clicked. */
  advanceButton?(scope: HTMLElement): HTMLElement | null;
  /** A gate's own button before the application (Jobvite's data consent):
   *  the step's advance whatever it says, since it sends no application.
   *  Not terminal-checked: return only a button known to send none. */
  gateAdvance?(scope: HTMLElement): HTMLElement | null;
  /** The step's advance is still on its way: the gate's button renders once
   *  the page has loaded what a choice asked for. The flow waits briefly. */
  advancePending?(scope: HTMLElement): boolean;
  /** The job posting's apply-entry button (leads INTO the application), when
   *  the site has a reliable selector (Workday: adventureButton). Only
   *  consulted while the page has no recognized form fields. */
  entryButton?(doc: Document): HTMLElement | null;
}
