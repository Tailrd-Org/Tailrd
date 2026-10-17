/**
 * Apply-entry discovery: the button/link on a job POSTING (or an apply-method
 * chooser) that leads INTO the application, "Apply", "Apply Manually",
 * "Continue Application". The flow clicks these to enter multi-page ATSs
 * (Workday: job page → Apply → Apply Manually → sign-in/create-account → form).
 *
 * Deliberately separate from advance.ts: advance buttons live INSIDE a detected
 * form scope; entry buttons only matter when the page has NO recognized fields
 * yet, so the two can never fight. On a filled form, "Apply"/"Submit" is a
 * terminal button (advance.ts) and is never clicked.
 *
 * Tier order mirrors the Jobright reference: an explicit "Apply Manually"
 * always beats "Autofill with Resume" / "Use My Last Application" on Workday's
 * chooser, our filler drives the manual form.
 */
import { cleanText, deepQueryAll, isVisible, shownText } from "./domUtils";
import type { SiteAdapter } from "./adapters/types";

export interface EntryButton {
  el: HTMLElement;
  label: string;
  /** True when a site adapter supplied it (known ATS, safe to auto-surface). */
  fromAdapter: boolean;
}

const ENTRY_SELECTOR = 'a[href], button, [role="button"], input[type="submit"], input[type="button"]';

/** Chooser option we always prefer (Workday's manual-application path; the
 *  guest path beside a sign-in, Dayforce's "Apply without an Account"). */
const ENTRY_MANUAL_RE =
  /^(apply manually|postuler manuellement|apply without (an )?account|(apply|continue) as (a )?guest|guest (apply|application))$/i;
/** The posting's own Apply button. Anchored, never a sentence containing "apply".
 *  "Apply for this job online" is iCIMS's on every posting (round 5 batch C). */
const ENTRY_APPLY_RE =
  /^(apply( now| online)?!?|apply (for|to) (this )?(job|position|role|opening)( online)?|easy apply|postuler( maintenant)?|poser (sa|ma|votre) candidature|candidater( maintenant)?)$/i;
/** Resume-an-application verbs (Workday shows these when a draft exists). */
const ENTRY_CONTINUE_RE = /^(continue application|continue your application|start( your)? application)$/i;
/** Arrows and chevrons that decorate a button's text: "Apply now »" (L3Harris
 *  on SuccessFactors, round 5 batch C) never matched the anchored Apply. */
const TRAILING_DECOR = /[\s>\u00bb\u203a\u2192\u2794\u25b6\u276f]+$/u;
/** Chooser options that bypass the manual form, never click these. */
const ENTRY_EXCLUDE_RE = /autofill with resume|use my last application|apply with (linkedin|indeed|seek)/i;

function isClickable(el: HTMLElement): boolean {
  if ((el as HTMLButtonElement).disabled) return false;
  if (el.getAttribute("aria-disabled") === "true") return false;
  return isVisible(el);
}

function entryText(el: HTMLElement): string {
  return (
    cleanText(el.getAttribute("aria-label")) ||
    cleanText(el.textContent) ||
    cleanText((el as HTMLInputElement).value ?? "")
  );
}

/**
 * Every name the control goes by: its aria-label AND what it shows. An
 * aria-label often names the job too ("Apply for Software Designer" on a
 * Dayforce button that reads "Apply"), so matching only the first name missed
 * the button. A bypass option named in either one still excludes it.
 *
 * What it shows is read without the parts the page hides (iCIMS's hidden short
 * label, see shownText). A title names the control only when it shows no
 * text of its own: beside visible text it is a tooltip, not the name.
 */
function entryTexts(el: HTMLElement): string[] {
  const shown = shownText(el);
  const texts = [
    cleanText(el.getAttribute("aria-label")),
    cleanText(el.textContent),
    shown,
    cleanText((el as HTMLInputElement).value ?? ""),
    shown ? "" : cleanText(el.getAttribute("title")),
  ]
    .map((t) => t.replace(TRAILING_DECOR, ""))
    .filter((t) => t && t.length <= 40);
  return [...new Set(texts)];
}

/**
 * The best apply-entry control on the page, or null.
 *
 * "Apply Manually" is checked FIRST (ahead of even the adapter hook) because
 * Workday's chooser opens as an in-page overlay: the URL never changes and the
 * posting's own `adventureButton` ("Apply") stays in the DOM, visible, behind
 * it. An adapter-first order therefore keeps answering "Apply" forever, which
 * (a) re-clicks the button that opened the chooser instead of picking a path
 * and (b) leaves stepSignature() identical across the transition, so the flow
 * concludes the page never changed and gives up. The manual path is also simply
 * the right answer whenever it is on screen, our filler drives that form.
 *
 * After that the adapter hook wins (reliable automation-ids), then the generic
 * tiers, which match anchored button text so body copy like "apply by June 1"
 * can never produce a click target.
 */
export function findApplyEntry(doc: Document, adapter: SiteAdapter | null): EntryButton | null {
  let apply: HTMLElement | null = null;
  let cont: HTMLElement | null = null;
  let applyLabel = "";
  let contLabel = "";
  for (const el of deepQueryAll(doc, ENTRY_SELECTOR)) {
    if (!isClickable(el)) continue;
    const texts = entryTexts(el);
    if (!texts.length || texts.some((t) => ENTRY_EXCLUDE_RE.test(t))) continue;
    const manual = texts.find((t) => ENTRY_MANUAL_RE.test(t));
    if (manual) return { el, label: manual, fromAdapter: false }; // best, stop
    const applyText = texts.find((t) => ENTRY_APPLY_RE.test(t));
    if (!apply && applyText) [apply, applyLabel] = [el, applyText];
    const contText = texts.find((t) => ENTRY_CONTINUE_RE.test(t));
    if (!cont && contText) [cont, contLabel] = [el, contText];
  }
  try {
    const fromAdapter = adapter?.entryButton?.(doc);
    if (fromAdapter && isClickable(fromAdapter)) {
      return { el: fromAdapter, label: entryText(fromAdapter) || "Apply", fromAdapter: true };
    }
  } catch {
    // Adapter hooks refine, never break, fall through to the generic tiers.
  }
  if (apply) return { el: apply, label: applyLabel, fromAdapter: false };
  return cont ? { el: cont, label: contLabel, fromAdapter: false } : null;
}
