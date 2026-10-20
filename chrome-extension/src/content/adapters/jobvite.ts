// chrome-extension/src/content/adapters/jobvite.ts
/**
 * Jobvite (`jobs.jobvite.com`). Its application forms fill through the
 * generic pipeline; this adapter owns the one page that pipeline cannot pass:
 * the "Data Consent" gate some companies put before the application (Nutanix,
 * live 2026-10-17).
 *
 * The gate is a form named "consentForm" that posts to the job's apply page:
 * a "Location of Residence and Language" select whose options are the
 * company's consent policies, then, once one is chosen and the page has loaded
 * it (a GET), the policy's text and its two controls, the accept button
 * (worded by the policy: Nutanix's reads "Submit") and a decline link. The
 * accept POSTs the consent and opens the application. It sends no
 * application, so it is the step's advance whatever it says (decision 70).
 * Live runs block that POST.
 */
import { isVisible } from "../domUtils";
import { ADAPTERS } from "./registry";
import { classifyByAttr, SOCIAL_URL_RULES } from "./shared";
import type { SiteAdapter } from "./types";

const JOBVITE_HOST = /(^|\.)jobvite\.com$/i;
const CONSENT_FORM = 'form[name="consentForm"]';
const SUBMIT = 'button[type="submit"], input[type="submit"]';

/**
 * The consent form around or inside `scope`, when it opens an application: its
 * action is a job's apply page ("/nutanix/job/oi8PAfwz/apply") or the general
 * one ("/nutanix/apply"). The same form also signs people up for job alerts
 * ("/nutanix/jobAlerts"), which is never an application's step.
 */
export function consentFormOf(scope: HTMLElement): HTMLFormElement | null {
  const form =
    scope.closest<HTMLFormElement>(CONSENT_FORM) ??
    scope.querySelector<HTMLFormElement>(CONSENT_FORM) ??
    scope.ownerDocument?.querySelector<HTMLFormElement>(CONSENT_FORM) ??
    null;
  if (!form) return null;
  const action = (form.getAttribute("action") ?? "").split(/[?#]/)[0];
  return /\/apply\/?$/i.test(action) ? form : null;
}

function enabled(el: HTMLElement): boolean {
  return !(el as HTMLButtonElement).disabled && el.getAttribute("aria-disabled") !== "true";
}

/** The consent form's accept button, once the policy shows it. */
function acceptButton(form: HTMLFormElement): HTMLElement | null {
  for (const el of form.querySelectorAll<HTMLElement>(SUBMIT)) {
    if (enabled(el) && isVisible(el)) return el;
  }
  return null;
}

export const jobviteAdapter: SiteAdapter = {
  id: "jobvite",
  label: "Jobvite",
  match: (host) => JOBVITE_HOST.test(host),
  classify: (ctx) => classifyByAttr(ctx.el, SOCIAL_URL_RULES, 0.95),
  gateAdvance: (scope) => {
    const form = consentFormOf(scope);
    return form ? acceptButton(form) : null;
  },
  // A policy is chosen, and the page is still loading it: its text and the
  // accept button appear a moment later.
  advancePending: (scope) => {
    const form = consentFormOf(scope);
    if (!form || acceptButton(form)) return false;
    return [...form.querySelectorAll<HTMLSelectElement>("select")].some((s) => s.value.trim() !== "");
  },
};

ADAPTERS.push(jobviteAdapter);
