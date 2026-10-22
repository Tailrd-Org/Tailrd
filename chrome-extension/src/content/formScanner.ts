/**
 * Page scanner: finds candidate form controls, groups radios, classifies
 * everything via the field matcher and maintains a registry that maps the
 * serializable field ids (sent to the popup) back to live DOM nodes.
 *
 * Dynamic ATS pages (Workday, Ashby…) re-render constantly, so a debounced
 * MutationObserver triggers rescans. Ids stay stable across rescans because
 * they are stored on the elements themselves (FIELD_ID_ATTR).
 */
import { FIELD_ID_ATTR } from "../shared/constants";
import type { ControlType, DetectedField, FieldCategory, UserApplicationProfile } from "../shared/types";
import {
  bestDisplayLabel,
  cleanText,
  collectSignals,
  deepQueryAll,
  EXTENSION_UI_HOST_IDS,
  hiddenFromReaders,
  isHiddenButLabeled,
  isPlaceholderFiller,
  isUploadAffordance,
  isRequiredField,
  isVisible,
  nearbyText,
  ownLabel,
  slottedQueryAll,
  type FieldSignals,
} from "./domUtils";
import { isConsentOption } from "./answerKind";
import { holdsPageGuess } from "./pageGuesses";
import { isCaptchaField } from "./captcha";
import { isConsentField } from "./consent";
import { isInPageChrome } from "./pageChrome";
import { isSiteSearch } from "./siteSearch";
import { filterToScope, resolveFormScope, type ScopeEntry } from "./formScope";
import { isAriaCombobox, readComboboxOptions, readComboboxValue } from "./comboboxEngine";
import { classifyWithAdapter, resolveAnswerWithAdapter } from "./adapters/apply";
import { resolveField, snapToOption, type FieldResolution } from "./fieldResolver";
import { splitGreenhouseDate } from "./adapters/greenhouse";
import { graduationOfRow, parseDateSpan, PRESENT_RE, profileFacts, type EducationEntryFacts } from "./profileFacts";
import { isConsentText, resolveCheckboxIntent } from "./checkboxIntent";
import { matchOption } from "./writeEngine";
import { getAdapter } from "./adapters/registry";
import { detectGroupIndex } from "./groupIndex";
import type { SiteAdapter } from "./adapters/types";
import { detectFillDriver, isLegacyReactSelect } from "./driverDetect";
import { DATE_PART_ID_SELECTOR, DATE_PART_SELECTOR } from "./adapters/workdaySelectors";
import type { FillDriver } from "./mainWorldBridge";
import { isDeclineText } from "./demographicMatch";
import { internationalNumber, looksLikeDialCodes, nationalNumber } from "./phoneNumber";

/** Live handle for a detected field, never leaves the content script. */
export interface RuntimeControl {
  id: string;
  controlType: ControlType;
  /** Single element controls. */
  el?: HTMLElement;
  /** Radio groups: all members, in DOM order. */
  radios?: HTMLInputElement[];
  /** Native checkbox groups ("select all that apply"): all members, in DOM order. */
  checkboxes?: HTMLInputElement[];
  /** For customDropdown/combobox: which MAIN-world driver fills it, if any. */
  driver?: FillDriver;
  /** A multi-select combobox (skills, multiple locations): its value is a list,
   *  added one chip at a time rather than matched as a single option. */
  multi?: boolean;
}

export interface ScanResult {
  fields: DetectedField[];
  registry: Map<string, RuntimeControl>;
  adapter: SiteAdapter | null;
  /** The resolved application-form container, or null when scoping fell back. */
  scopeEl: HTMLElement | null;
}

/**
 * Ashby's Boolean question: two buttons with aria-pressed ("Yes", "No") over a
 * hidden checkbox, in a container with Ashby's public class. The question's
 * <label for> names the checkbox's name, not an id, so the checkbox alone
 * read "No" as its label, and no Yes/No question on Ashby (work right,
 * sponsorship, relocation, U.S. person) was ever answered (live 2026-10-08).
 * The container is scanned as an ARIA radio group of its buttons.
 */
const PRESSED_GROUP = ".ashby-application-form-input-yesno";

const CANDIDATE_SELECTOR = [
  "input",
  "textarea",
  "select",
  '[contenteditable="true"]',
  '[role="textbox"]',
  // ARIA comboboxes / custom dropdowns (react-select, Headless UI, Workday…).
  // Driven by opening the listbox and clicking an option (see comboboxEngine).
  '[role="combobox"]',
  '[aria-haspopup="listbox"]',
  // ARIA radio groups (react-aria / Radix custom radios, Jobvite, etc.): a
  // role=radiogroup whose role=radio children are divs, not native inputs.
  '[role="radiogroup"]',
  // Ashby's Yes/No: two aria-pressed buttons over a hidden checkbox (see
  // PRESSED_GROUP).
  PRESSED_GROUP,
].join(", ");

/** Input types that are never application fields. `password` is intentionally
 *  NOT here: it is surfaced as an `accountPassword` field, filled only by the
 *  account sub-flow (see controlTypeOf + scanPage below), never generically. */
const SKIPPED_INPUT_TYPES = new Set([
  "hidden",
  "submit",
  "button",
  "reset",
  "image",
  "search",
  "range",
  "color",
]);

/** Controls whose options are fully known at scan time, a deterministic
 *  profile value that matches none of them can only fail to fill, so it is
 *  dropped and the field routes to the option-aware AI pass. Comboboxes /
 *  custom dropdowns are excluded: they harvest their real options lazily. */
const CONSTRAINED_OPTION_TYPES: ReadonlySet<ControlType> = new Set<ControlType>([
  "select",
  "radioGroup",
  "checkboxGroup",
  "ariaRadioGroup",
]);

/** The DetectedField flags a resolution sets (absent when false, so field
 *  snapshots in existing tests and telemetry stay unchanged). */
function resolutionFlags(
  r: FieldResolution
): Pick<DetectedField, "deterministic" | "deviceAbstained" | "answerKind" | "dateFormat"> {
  return {
    ...(r.source === "question" && r.value !== null ? { deterministic: true } : {}),
    ...(r.deviceAbstained ? { deviceAbstained: true } : {}),
    answerKind: r.kind,
    ...(r.dateFormat ? { dateFormat: r.dateFormat } : {}),
  };
}

/** Null out a proposed value that cannot land in a constrained-option control
 *  (e.g. the applicant's home city into a select of company offices). */
function guardConstrainedOption(
  value: string | null,
  controlType: ControlType,
  options: string[] | undefined
): string | null {
  if (value === null || !CONSTRAINED_OPTION_TYPES.has(controlType)) return value;
  if (!options || options.length === 0) return value;
  return matchOption(options, (o) => o, (o) => o, value) ? value : null;
}

let idCounter = 0;

/** Stable per-frame token so field ids are unique across iframes. */
export const FRAME_TOKEN = Math.random().toString(36).slice(2, 8);

/** Ids assigned in the current scanPage() run, lets ensureFieldId fall back to
 *  a counter when a deterministic id would collide with another live field. */
const assignedThisScan = new Set<string>();

/** Identifiers that carry a volatile per-render instance counter / uuid
 *  (react-select "react-select-3-input", SAP juic "36:_input", uuids), a
 *  "stable" id built from these would NOT survive a re-render, so we skip them. */
const VOLATILE_ID = /react-select-\d|(^|[^0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-|^\d+:/i;

/** A re-render-stable identity for `el` (Workday keeps element id / name /
 *  automation-id across step re-renders), or null when it only has volatile ones. */
function stableIdentity(el: HTMLElement): string | null {
  const candidates: Array<[string, string]> = [
    ["id", el.id],
    ["name", el.getAttribute("name") ?? ""],
    ["auto", el.getAttribute("data-automation-id") ?? ""],
  ];
  for (const [kind, value] of candidates) {
    if (value && !VOLATILE_ID.test(value)) return `${kind}=${value}`;
  }
  return null;
}

function hashKey(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

/**
 * A per-field id that is STABLE across re-renders. Workday swaps the whole field
 * subtree on every step, so a counter id would change and orphan the reconciler's
 * state as "Field no longer found". Deriving the id from a stable identifier
 * (element id / name / automation-id) makes a replaced element resolve to the
 * same field. Falls back to a counter for fields with no stable identifier (and
 * on the rare collision between two live fields sharing an identifier).
 */
function ensureFieldId(el: HTMLElement): string {
  const existing = el.getAttribute(FIELD_ID_ATTR);
  if (existing) {
    assignedThisScan.add(existing);
    return existing;
  }
  const stable = stableIdentity(el);
  let id = stable ? `${FRAME_TOKEN}-s${hashKey(stable)}` : null;
  if (!id || assignedThisScan.has(id)) id = `${FRAME_TOKEN}-${idCounter++}`;
  assignedThisScan.add(id);
  el.setAttribute(FIELD_ID_ATTR, id);
  return id;
}

function controlTypeOf(el: HTMLElement): ControlType | null {
  // ARIA combobox / listbox dropdown, checked first so a react-select
  // <input role="combobox"> is driven by the listbox engine, not typed into,
  // and a Workday <button aria-haspopup="listbox"> is now fillable.
  if (isAriaCombobox(el)) return "combobox";
  // ARIA radio group (role=radio children clicked to select), checked before the
  // generic element fallbacks so it is driven as a choice control, not skipped.
  if (el.getAttribute("role") === "radiogroup") return "ariaRadioGroup";
  if (el.matches(PRESSED_GROUP)) return "ariaRadioGroup";
  if (el instanceof HTMLInputElement) {
    if (el.type === "password") return "password"; // account sub-flow only
    if (SKIPPED_INPUT_TYPES.has(el.type)) return null;
    if (el.type === "checkbox") return "checkbox";
    if (el.type === "radio") return "radioGroup"; // grouped later
    if (el.type === "file") return "file";
    // Workday's multiselect/typeahead trigger (e.g. Country Phone Code) is a bare
    // <input> with NO role=combobox / aria-haspopup, its widget type lives only
    // in data-uxi-widget-type="selectinput". Drive it through the listbox engine
    // instead of typing the value into what is actually a search box.
    if (el.getAttribute("data-uxi-widget-type") === "selectinput") return "combobox";
    // react-select before v5 has no role=combobox either, only
    // aria-autocomplete="list" (see isLegacyReactSelect).
    if (el.getAttribute("aria-autocomplete") === "list" && isLegacyReactSelect(el)) return "combobox";
    return "text"; // text, email, tel, url, number, date…
  }
  if (el instanceof HTMLTextAreaElement) return "textarea";
  if (el instanceof HTMLSelectElement) return "select";
  if (el.tagName === "BUTTON") return "customDropdown";
  if (el.isContentEditable || el.getAttribute("role") === "textbox") return "contenteditable";
  return null;
}

/** A dropdown input that names the list it opens, and is not itself declared read-only. */
function opensList(el: HTMLElement): boolean {
  if (el.getAttribute("aria-readonly") === "true") return false;
  return (
    (el.getAttribute("aria-haspopup") || "").toLowerCase() === "listbox" ||
    !!el.getAttribute("aria-controls") ||
    !!el.getAttribute("aria-owns")
  );
}

/**
 * Every option a control offers, for resolution: the panel keeps a capped copy
 * (60), and an answer past the cap was "not offered". A combobox list read
 * whole too: Schwab's 245 dialing codes on iCIMS were cut at Canada, then the
 * only "+1" left, and a Boston number got "(+1) Canada" (live 2026-10-17).
 */
function everyOption(el: HTMLElement, controlType: ControlType, capped: string[] | undefined): string[] | undefined {
  if (el instanceof HTMLSelectElement) return selectOptions(el, Infinity);
  if (controlType === "combobox") return readComboboxOptions(el, Infinity) ?? capped;
  return capped;
}

/** Options for a <select>, trimmed for transport. Exported for the Phase-2
 *  re-ask pass, which re-reads options after dependent-dropdown repopulation. */
export function selectOptions(el: HTMLSelectElement, limit = 60): string[] {
  return Array.from(el.options)
    .map((o) => cleanText(o.textContent))
    .filter((t) => t.length > 0)
    .slice(0, limit);
}

/** Option labels of an ARIA radio group (its role=radio children). */
/** Every option of a choice group is read: 50 states as radios lost Texas
 *  (the 43rd) to a 30-option cap (Kenect on Breezy, live 2026-10-03). The
 *  bound is only a guard against runaway markup. */
const MAX_GROUP_OPTIONS = 300;

function ariaRadioOptions(group: HTMLElement): string[] {
  // Slotted radios too, named by their own label attribute: SmartRecruiters'
  // <spl-radio label="Yes"> are slotted into the group its shadow root draws
  // (screening step, live, round 7: no options read).
  const radios = slottedQueryAll(group, '[role="radio"]');
  return (radios.length ? radios : Array.from(group.querySelectorAll<HTMLElement>("button[aria-pressed]")))
    .map((r) => cleanText(r.getAttribute("aria-label")) || cleanText(r.getAttribute("label")) || cleanText(r.textContent))
    .filter((t) => t.length > 0)
    .slice(0, MAX_GROUP_OPTIONS);
}

/**
 * A choice group nothing names but its options: the standard self-ID answer
 * sets say which question they answer. Agiloft's disability radios on Lever
 * carry no label, legend or question text, so the group was "unknown" and
 * stayed blank for every profile (2026-10-03).
 */
/**
 * A group's options can name the question its label does not ("Please check
 * one of the boxes below:" over three disability boxes). They decide an
 * unknown question, and they LIFT a weak reading that agrees with them: the
 * Workday disability form's own heading nearby read "disability" at 0.66,
 * under the fill threshold, so the boxes were never chosen to fill.
 */
function withOptionsEvidence(
  current: { category: FieldCategory; confidence: number; sensitive: boolean },
  options: string[],
  label = ""
): { category: FieldCategory; confidence: number; sensitive: boolean } {
  const consent = consentOptionsOverride(current, options);
  if (consent !== current) return consent;
  const named = categoryOfOptions(options, label);
  if (!named) return current;
  if (current.category === "unknown") return named;
  if (current.category === named.category && current.confidence < named.confidence) return named;
  // Self-ID options outrank a paragraph's stray words: SRC's VEVRAA notice
  // ("…entitled to compensation…") read as salary over "I AM NOT A
  // PROTECTED VETERAN" | "I IDENTIFY AS ONE OR MORE…" (Jobvite bank 2026-10-17).
  if (!current.sensitive) return named;
  // Of two self-ID topics, the options say which is asked: Veolia's race list
  // sits under its EEO paragraph ("…gender, gender identity or expression…")
  // and was read as gender identity, blank for everyone (SmartRecruiters
  // bank, round 7).
  // (A gender identity asked over Man | Woman | … stays gender identity: the
  // label is the narrower of the two.)
  const genderFamily = (c: FieldCategory): boolean => c === "eeoGender" || c === "eeoGenderIdentity";
  if (named.sensitive && named.category !== current.category && !(genderFamily(named.category) && genderFamily(current.category))) return named;
  return current;
}

/** Controls that answer by choosing. */
const CHOOSING: ReadonlySet<ControlType> = new Set<ControlType>(["select", "combobox", "customDropdown", "radioGroup", "ariaRadioGroup", "checkbox", "checkboxGroup"]);

/**
 * A control that answers by choosing is never the résumé upload: "If
 * requested, would you be able to provide documentation reflecting the
 * education and employment history listed on your resume?" (Justworks on
 * Greenhouse, live 2026-10-17) was the upload by its "resume", and no rule
 * ever read it. A question, then, unknown until a rule reads it.
 */
function choiceIsNoUpload(
  current: { category: FieldCategory; confidence: number; sensitive: boolean },
  controlType: ControlType
): { category: FieldCategory; confidence: number; sensitive: boolean } {
  return current.category === "resumeUpload" && CHOOSING.has(controlType) ? { category: "unknown", confidence: 0, sensitive: false } : current;
}

/**
 * Options that only consent or refuse ("I consent" | "I do not Consent", "I
 * agree to these expectations") make an acknowledgement, whatever its
 * paragraph mentions. JazzHR's ended "…a qualified individual with a
 * disability…ADA", was read as the disability question, and the profile's
 * "No" ticked "I do not Consent" (Directors Investment Group, live
 * 2026-10-05); Block's interview expectations read as a last name.
 */
function consentOptionsOverride(
  current: { category: FieldCategory; confidence: number; sensitive: boolean },
  options: string[]
): { category: FieldCategory; confidence: number; sensitive: boolean } {
  const real = options.filter((o) => o.trim() && !isPlaceholderFiller(o));
  if (current.category !== "unknown" && real.length > 0 && real.every(isConsentOption)) {
    return { category: "unknown", confidence: current.confidence, sensitive: false };
  }
  return current;
}

function categoryOfOptions(options: string[], label = ""): { category: FieldCategory; confidence: number; sensitive: boolean } | null {
  const count = (re: RegExp): number => options.filter((o) => re.test(o.toLowerCase())).length;
  if (count(/\bdisabilit(y|ies)\b/) >= 2) return { category: "eeoDisability", confidence: 0.9, sensitive: true };
  if (count(/\bprotected veterans?\b/) >= 2) return { category: "eeoVeteran", confidence: 0.9, sensitive: true };
  // Races as the options: "Please select an identity (or multiple) that best
  // represents you:" over Black | White | East Asian | … (Wattpad on Lever,
  // live 2026-10-08) named no race in its label and went to the AI.
  // Not a follow-up: "If you selected 'Two or more races', please check all
  // racial categories…" (SoFi) asks only those who did.
  if (!/^\s*if\b/i.test(label) && count(/^(white|black|caucasian|african american|hispanic|latin[oaex]|asian|east asian|south asian|southeast asian|indigenous|native american|pacific islander|middle eastern|arab)\b/) >= 4) {
    return { category: "eeoRace", confidence: 0.9, sensitive: true };
  }
  // A man's and a woman's option: "Please select" over Male | Female |
  // Non-binary | I do not wish to identify (Veolia, SmartRecruiters bank,
  // round 7) named no gender in its label, and took its decline.
  if (count(/^\s*(cis(gender)? )?(male|man)\s*(\(.*\))?\s*$/) >= 1 && count(/^\s*(cis(gender)? )?(female|woman)\s*(\(.*\))?\s*$/) >= 1 && options.filter((o) => o.trim()).length <= 8) {
    return { category: "eeoGender", confidence: 0.9, sensitive: true };
  }
  return null;
}

/** The label of one radio button or checkbox: its own label, else the text
 *  printed right after it, else its value (never the browser's default "on"). */
function radioOptionLabel(radio: HTMLInputElement): string {
  const own = ownLabel(radio);
  if (own) return cleanText(own.textContent);
  const aria = cleanText(radio.getAttribute("aria-label"));
  if (aria) return aria;
  // No <label>: Breezy prints <input type="checkbox"><span>C#</span>, and every
  // option read "on" (Vagaro, live 2026-10-03).
  const after = textAfterBox(radio);
  if (after) return after;
  return radio.value && radio.value !== "on" ? radio.value : "";
}

/** The text between a box and the next control, within its parent. */
function textAfterBox(box: HTMLInputElement): string {
  let text = "";
  for (let node = box.nextSibling; node; node = node.nextSibling) {
    if (node.nodeType === Node.ELEMENT_NODE && ((node as Element).matches("input, select, textarea, button") || (node as Element).querySelector("input, select, textarea"))) break;
    text += ` ${node.textContent ?? ""}`;
    if (text.length > 120) break;
  }
  const t = cleanText(text);
  return t.length <= 120 ? t : "";
}

/**
 * The question of an option group that has no fieldset/radiogroup container:
 * climb from the options' common ancestor and take the first text that sits
 * OUTSIDE the options, the block's question.
 *
 * Lever renders every custom radio question as
 *   <li class="application-question"><div class="application-label">Question?</div>
 *     <div class="application-field"><ul><li><label><input type=radio>Yes</label></li>…
 * where nearbyText from the first radio climbs three levels of empty siblings
 * and gives up, so the group was labelled with its input's NAME
 * ("cards[f6189244-…][field0]") and never recognized: "Are you legally able to
 * work in Canada?" and the years-of-experience buckets went unanswered (live,
 * 2026-10-03).
 */
function questionAboveOptions(members: HTMLInputElement[]): string {
  if (members.length === 0) return "";
  let common: HTMLElement | null = members[0].parentElement;
  while (common && !members.every((m) => common!.contains(m))) common = common.parentElement;
  const optionText = new Set(members.map((m) => cleanText(radioOptionLabel(m))).filter(Boolean));
  for (let node = common, depth = 0; node && depth < 5 && !CONTAINER_CLIMB_BOUNDARY.has(node.tagName); node = node.parentElement, depth++) {
    let text = "";
    for (const child of Array.from(node.children)) {
      if (members.some((m) => child.contains(m))) continue;
      if (child.querySelector(OTHER_FIELD_SELECTOR) || child.matches(OTHER_FIELD_SELECTOR)) continue;
      text += ` ${child.textContent ?? ""}`;
    }
    const t = cleanText(text);
    if (t && t.length <= 300 && !optionText.has(t) && !isPlaceholderFiller(t)) return t;
    // Longer: a question with its description (Lever's <div class="text">
    // Question?</div><p class="description">…</p>, eqbank's "racialized
    // person"), or a question a paragraph long (Kobie's 30 states). The
    // block's first part is the question (live 2026-10-08: both were labelled
    // with the input's name).
    if (t.length > 300) {
      for (const child of Array.from(node.children)) {
        if (members.some((m) => child.contains(m))) continue;
        if (child.querySelector(OTHER_FIELD_SELECTOR) || child.matches(OTHER_FIELD_SELECTOR)) continue;
        const head = firstTextPart(child);
        if (head && !optionText.has(head) && !isPlaceholderFiller(head)) return head;
      }
    }
    // Another control's block: stop before borrowing a neighbour's question.
    if (node.parentElement && Array.from(node.parentElement.querySelectorAll(OTHER_FIELD_SELECTOR)).some((c) => !members.includes(c as HTMLInputElement) && !node!.contains(c))) {
      break;
    }
  }
  return "";
}

/** A block's first part with text, up to a paragraph long: its own text when
 *  it has no element inside, else its first child with text. */
function firstTextPart(el: Element, depth = 0): string {
  const own = cleanText(el.textContent);
  if (!own) return "";
  const kids = Array.from(el.children).filter((c) => cleanText(c.textContent));
  // Text of its own beside the children ("Question? <span>✱</span>"): the whole.
  const loose = Array.from(el.childNodes).some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").trim());
  if (kids.length === 0 || loose || depth >= 3) return own.length <= 800 ? own : "";
  return firstTextPart(kids[0], depth + 1);
}

/** Inputs a user fills in (choices included); hidden mirrors and buttons are not. */
const USER_INPUT_SELECTOR =
  'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="image"]), select, textarea';

/** A container with two or more visible inputs besides the group's own is a
 *  section of the form, not the group's question (one "Other: ___" box beside
 *  the options still leaves it the group's). */
function isSectionContainer(container: Element, members: HTMLInputElement[]): boolean {
  let others = 0;
  for (const c of container.querySelectorAll<HTMLElement>(USER_INPUT_SELECTOR)) {
    if (members.includes(c as HTMLInputElement) || !isVisible(c)) continue;
    if (++others >= 2) return true;
  }
  return false;
}

/**
 * Signals for a group come from its container (fieldset legend, role=group/
 * radiogroup label, or (for a container with none of those) the heading text
 * immediately before it) rather than the individual buttons.
 */
function groupSignals(members: HTMLInputElement[], container: Element | null): FieldSignals {
  const first = members[0];
  let label = "";
  // A <fieldset> holding other questions is a SECTION: its legend ("3.
  // Questions", Pinpoint, live 2026-10-03) named every radio question in it,
  // and each was answered from its category without being read. The question
  // above the options is the group's own.
  if (container && isSectionContainer(container, members)) container = null;
  if (container) {
    const legend = container.querySelector("legend");
    label = cleanText(legend?.textContent) || cleanText(container.getAttribute("aria-label"));
    if (!label) {
      const ids = container.getAttribute("aria-labelledby");
      if (ids) {
        label = cleanText(
          ids
            .split(/\s+/)
            .map((id) => document.getElementById(id)?.textContent ?? "")
            .join(" ")
        );
      }
    }
    // A <label> inside the group that labels none of its options is the
    // group's question. Ashby renders every radio question this way:
    // <fieldset><label for="<question id>">Question</label> + options, with no
    // <legend>. Without this the label fell through to nearbyText(container),
    // the text BEFORE the fieldset, which is the PREVIOUS question: "Do you
    // think AI will take over the world?" was classified as a work-authorization
    // question and answered "Yes" (live, 2026-10-03).
    if (!label) {
      const optionLabels = new Set<Element>();
      for (const m of members) for (const l of Array.from(m.labels ?? [])) optionLabels.add(l);
      const heading = Array.from(container.querySelectorAll("label")).find(
        (l) => !optionLabels.has(l) && !l.querySelector("input, select, textarea") && cleanText(l.textContent)
      );
      if (heading) label = cleanText(heading.textContent);
    }
    // No semantic label: a plain-<div> group's question is usually the heading
    // text right before the option list (the container itself, not the first
    // option, since the first option has no useful "previous sibling" text).
    // Lever wraps the list in its own field box, beside the question's: the
    // question above the options, as for a radio group (Zoox, live 2026-10-03:
    // labelled "cards[…][field0]").
    if (!label) label = nearbyText(container as HTMLElement) || questionAboveOptions(members);
  } else {
    label = questionAboveOptions(members);
  }
  const base = collectSignals(first);
  return {
    ...base,
    // The group question; individual radio/checkbox labels ("Yes"/"LinkedIn") are options.
    label: label || base.nearby,
    placeholder: "",
    typeHint: "",
  };
}

/**
 * A native <select> enhanced by select2 / chosen: hidden (and often
 * aria-hidden), mirrored by a styled proxy. Lever's university picker is one
 * (live 2026-10-03): the scanner skipped the hidden select and fought the proxy,
 * so "What Post-Secondary institution do you attend?" stayed blank.
 */
function isEnhancedSelect(el: HTMLElement): boolean {
  if (!(el instanceof HTMLSelectElement)) return false;
  if (el.classList.contains("select2-hidden-accessible")) return true;
  const next = el.nextElementSibling;
  return Boolean(next && /\b(select2-container|chosen-container)\b/.test(next.getAttribute("class") ?? ""));
}

/** The styled proxy of an enhanced select, or its open dropdown/search box. */
function isEnhancedSelectProxy(el: HTMLElement): boolean {
  if (el.closest(".select2-dropdown, .chosen-drop")) return true;
  const container = el.closest(".select2-container, .chosen-container");
  if (!container) return false;
  const prev = container.previousElementSibling;
  return prev instanceof HTMLSelectElement && isEnhancedSelect(prev);
}

/**
 * Keys for radios that carry no `name`: the smallest container holding two or
 * more radios and no other kind of field (a fieldset / radiogroup when there is
 * one). One key per container, so its radios form one question.
 */
function namelessRadioKeys(): { keyFor(el: HTMLInputElement): string | null } {
  const keys = new Map<Element, string>();
  let n = 0;
  const otherField = 'input:not([type="radio"]):not([type="hidden"]):not([type="submit"]):not([type="button"]), select, textarea';
  return {
    keyFor(el: HTMLInputElement): string | null {
      let container: Element | null = el.closest('fieldset, [role="radiogroup"]');
      if (!container || container.querySelectorAll('input[type="radio"]').length < 2) {
        container = null;
        let node: Element | null = el.closest("label") ?? el.parentElement;
        for (let depth = 0; depth < 5 && node && !CONTAINER_CLIMB_BOUNDARY.has(node.tagName); depth++) {
          if (node.querySelectorAll('input[type="radio"]').length >= 2) {
            if (node.querySelectorAll(otherField).length === 0) container = node;
            break;
          }
          node = node.parentElement;
        }
      }
      if (!container) return null;
      let key = keys.get(container);
      if (!key) {
        key = `nameless-${n++}`;
        keys.set(container, key);
      }
      return key;
    },
  };
}

/** Form-field types other than checkboxes, finding one inside a candidate
 *  checkbox-group container means we've climbed past the group's natural
 *  boundary into an unrelated section. */
const OTHER_FIELD_SELECTOR =
  'input:not([type="checkbox"]):not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]), select, textarea';

/** Never accepted as a checkbox-group container even if it would otherwise qualify. */
const CONTAINER_CLIMB_BOUNDARY = new Set(["FORM", "BODY", "HTML"]);

/**
 * The smallest enclosing container for a "select all that apply" checkbox
 * cluster. Prefers an explicit `fieldset`/`[role=group]`; most real ATS render
 * the same pattern with plain `<div>`s instead, so fall back to the closest
 * ancestor (within a few levels, never the form/body/page itself) that encloses
 * ≥2 checkboxes and no unrelated field, the natural list boundary.
 */
function checkboxGroupContainer(el: HTMLInputElement): Element | null {
  const explicit = el.closest('fieldset, [role="group"]');
  if (explicit && explicit.querySelectorAll('input[type="checkbox"]').length >= 2) {
    return explicit;
  }
  let node: Element | null = el.closest("label") ?? el.parentElement;
  for (let depth = 0; depth < 5 && node && !CONTAINER_CLIMB_BOUNDARY.has(node.tagName); depth++) {
    if (
      node.querySelectorAll('input[type="checkbox"]').length >= 2 &&
      node.querySelectorAll(OTHER_FIELD_SELECTOR).length === 0
    ) {
      return node;
    }
    node = node.parentElement;
  }
  return null;
}

const UPLOAD_VERB_RE = /\b(upload|attach|add|choose|browse|select|drag)\b/i;
const RESUME_NOUN_RE = /\b(resume|r[ée]sum[ée]|cv|curriculum\s*vitae)\b/i;
const COVER_NOUN_RE = /\bcover\s*letter\b/i;

/** Accessible name of a clickable: aria-label, else the joined text of its
 *  aria-labelledby targets, else its own text. Capped for sanity. */
function accessibleNameOf(el: HTMLElement): string {
  const aria = cleanText(el.getAttribute("aria-label"));
  if (aria) return aria;
  const ids = el.getAttribute("aria-labelledby");
  if (ids) {
    const doc = el.ownerDocument;
    const txt = ids
      .split(/\s+/)
      .map((id) => cleanText(doc.getElementById(id)?.textContent))
      .filter(Boolean)
      .join(" ");
    if (txt) return txt;
  }
  return cleanText(el.textContent);
}

/**
 * Custom (non-`<input>`) résumé / cover-letter upload widgets. Some ATS, SAP
 * SuccessFactors most notably: render the upload as a `<div role="button">Upload
 * a Resume</div>` that opens a file dialog, with no scannable `<input type=file>`.
 * The generic loop never sees these (a role=button div isn't a form control), so
 * the panel reported "no résumé field" and its Attach button stayed disabled.
 *
 * This detects at most one widget per category, requiring BOTH an upload verb and
 * a résumé/cover noun in the accessible name so ordinary buttons never match, and
 * skips a category a native file input already covers (no duplicates). Emitted as
 * a `file` control the panel's Attach flow already understands. `deepQueryAll`
 * excludes our own panel, so its "Attach"/"Generate Custom Resume" buttons can't
 * be mistaken for a page field.
 */
function scanCustomUploads(fields: DetectedField[], registry: Map<string, RuntimeControl>): void {
  const covered = new Set<FieldCategory>(
    fields.filter((f) => f.controlType === "file").map((f) => f.category)
  );
  const seenWidgets = new Set<Element>();
  for (const el of deepQueryAll(document, '[role="button"], button')) {
    const name = accessibleNameOf(el);
    if (!name || name.length > 120 || !UPLOAD_VERB_RE.test(name)) continue;
    const category: FieldCategory | null = COVER_NOUN_RE.test(name)
      ? "coverLetter"
      : RESUME_NOUN_RE.test(name)
        ? "resumeUpload"
        : null;
    if (!category || covered.has(category)) continue;
    // One field per widget: SF renders several role=button parts (icon + label).
    const widget = el.closest('[class*="attach" i], [class*="upload" i], [class*="dropzone" i]') ?? el;
    if (seenWidgets.has(widget)) continue;
    seenWidgets.add(widget);
    covered.add(category);
    const id = ensureFieldId(el);
    registry.set(id, { id, controlType: "file", el });
    fields.push({
      id,
      category,
      confidence: 0.9,
      label: name.slice(0, 80),
      controlType: "file",
      required: false,
      proposedValue: null,
      fillable: false,
      sensitive: false,
      note: noteFor("file", category),
      currentValue: undefined,
    });
  }
}

const REPEAT_CATEGORIES: ReadonlyArray<ReadonlySet<FieldCategory>> = [
  new Set<FieldCategory>([
    "currentCompany",
    "currentTitle",
    "experienceStartDate",
    "experienceEndDate",
    "experienceDescription",
    "experienceCurrent",
  ]),
  new Set<FieldCategory>(["school", "degree", "fieldOfStudy", "graduationYear"]),
];

/** The field a row is recognized by, in order of preference. */
const ROW_ANCHORS: ReadonlyArray<ReadonlyArray<FieldCategory>> = [
  ["school", "degree"],
  ["currentCompany", "currentTitle"],
];

/**
 * Rows told apart by POSITION. Ashby repeats the same ids in every education
 * row (`_systemfield_education_history-degree` twice) and numbers none, so
 * both rows read as "no row": each took the most recent school, and the first
 * row's graduation year (Superhuman, live 2026-10-03). When a row's anchor
 * (School, Company) appears more than once as identical copies, each copy's
 * row is the largest region around it holding no other copy, and the
 * unnumbered row fields inside take that row's index. A region holding only
 * its anchor is no row (a flat layout cannot be split this way): nothing is
 * assigned then. Runs before the education-date pass, which reads the index.
 */
function assignRowsByPosition(
  fields: DetectedField[],
  registry: Map<string, RuntimeControl>,
  profile: UserApplicationProfile | null,
  adapter: SiteAdapter | null,
  fillEEO: boolean
): void {
  if (!profile) return;
  const elOf = (f: DetectedField): HTMLElement | null => {
    const c = registry.get(f.id);
    return c?.el ?? c?.radios?.[0] ?? c?.checkboxes?.[0] ?? null;
  };
  const rowCategories = new Set<FieldCategory>(REPEAT_CATEGORIES.flatMap((s) => [...s]));
  for (const anchors of ROW_ANCHORS) {
    const loose = fields.filter((f) => f.groupIndex == null && rowCategories.has(f.category) && elOf(f));
    for (const anchor of anchors) {
      const signature = (f: DetectedField): string => {
        const el = elOf(f) as HTMLElement;
        return [f.controlType, f.label.trim().toLowerCase(), el.id, el.getAttribute("name") ?? "", el.getAttribute("placeholder") ?? ""].join("|");
      };
      // The copies are the anchor's identical repeats. A lone question of the
      // same kind beside the rows (Superhuman's Yes/No "Are you currently
      // attending a college or university in the US?") is no copy.
      const bySignature = new Map<string, DetectedField[]>();
      for (const f of loose) {
        if (f.category !== anchor) continue;
        const key = signature(f);
        bySignature.set(key, [...(bySignature.get(key) ?? []), f]);
      }
      const repeated = [...bySignature.values()].filter((group) => group.length >= 2);
      if (repeated.length !== 1) continue;
      const copies = repeated[0];
      const anchorEls = copies.map((f) => elOf(f) as HTMLElement);
      const regions = anchorEls.map((a) => {
        let region = a;
        for (let p = a.parentElement; p; p = p.parentElement) {
          const up = p;
          if (anchorEls.some((o) => o !== a && up.contains(o))) break;
          region = up;
        }
        return region;
      });
      const members = regions.map((r, i) => loose.filter((f) => f !== copies[i] && r.contains(elOf(f))));
      if (members.some((m) => m.length === 0)) continue;
      regions.forEach((_, i) => {
        for (const f of [copies[i], ...members[i]]) {
          f.groupIndex = i;
          reresolveRowField(f, registry, profile, adapter, fillEEO);
        }
      });
      break;
    }
  }
}

/** A row's own place and pay: the school's or employer's, never the applicant's. */
const ROW_OWN_DETAIL = new Set<FieldCategory>(["addressStreet", "addressCity", "addressState", "postalCode", "location", "country", "salary"]);

/** Profile rows, most recent first: a running job or program, then by end
 *  date, then by start date. Unparsable dates keep their listed order. */
function rowsByRecency(rows: Array<{ start?: string; end?: string }>): number[] {
  const when = (text: string | undefined): number => {
    if (!text?.trim()) return -Infinity;
    if (PRESENT_RE.test(text)) return Infinity;
    const span = parseDateSpan(text.trim());
    return span ? span.latest.getTime() : -Infinity;
  };
  return rows
    .map((r, i) => ({ i, end: when(r.end), start: when(r.start) }))
    .sort((a, b) => b.end - a.end || b.start - a.start || a.i - b.i)
    .map((r) => r.i);
}

/**
 * Rows in a FLAT questionnaire: JazzHR asks "Education: Institution Name?",
 * "Location?", "Major/Minor?", then "Institution Name?" again, each a question
 * of its own with its own id, no row container, no index. Both schools were
 * the first school, both employers the current one, and an employer's
 * "Address?" got the applicant's home street (Directors Investment Group,
 * live 2026-10-05). When a row's anchor (School, Employer) is asked more than
 * once, each anchor starts a row, most recent first; the questions up to the
 * next anchor belong to it. A row's place and pay are left blank.
 */
function assignQuestionnaireRows(
  fields: DetectedField[],
  registry: Map<string, RuntimeControl>,
  profile: UserApplicationProfile | null,
  adapter: SiteAdapter | null,
  fillEEO: boolean
): void {
  if (!profile) return;
  const elOf = (f: DetectedField): HTMLElement | null => {
    const c = registry.get(f.id);
    return c?.el ?? c?.radios?.[0] ?? c?.checkboxes?.[0] ?? null;
  };
  const loose = fields
    .filter((f) => f.groupIndex == null && elOf(f))
    .sort((a, b) => ((elOf(a) as HTMLElement).compareDocumentPosition(elOf(b) as HTMLElement) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
  const kinds: Array<{ anchor: FieldCategory; members: ReadonlySet<FieldCategory>; order: number[] }> = [
    {
      anchor: "school",
      members: REPEAT_CATEGORIES[1],
      order: rowsByRecency((profile.education ?? []).map((e) => ({ end: e.graduationYear }))),
    },
    {
      anchor: "currentCompany",
      members: REPEAT_CATEGORIES[0],
      order: rowsByRecency((profile.experience ?? []).map((e) => ({ start: e.startDate, end: e.endDate }))),
    },
  ];
  const anchorOf = new Set<FieldCategory>(kinds.map((k) => k.anchor));
  // The same question asked again: "Education: Institution Name?" and
  // "Institution Name?". A university and a high school are two questions
  // (Palantir's "University" / "High School Name"), never two rows.
  const core = (label: string): string =>
    label
      .toLowerCase()
      .replace(/[*✱?:.()#\d]/g, " ")
      .replace(/\b(education|employment|employer history|work history|previous|prior|most recent|current|first|second|third|other|additional|another|next)\b/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  for (const kind of kinds) {
    const anchors = loose.filter((f) => f.category === kind.anchor);
    if (anchors.length < 2) continue;
    if (new Set(anchors.map((a) => core(a.label))).size !== 1 || anchors.some((a) => /\b(high|secondary) school\b/i.test(a.label))) continue;
    anchors.forEach((anchor, k) => {
      const row = kind.order[k];
      const from = loose.indexOf(anchor);
      const stop = k + 1 < anchors.length ? loose.indexOf(anchors[k + 1]) : loose.length;
      const members: DetectedField[] = [anchor];
      for (let i = from + 1; i < stop; i++) {
        const f = loose[i];
        if (anchorOf.has(f.category) && f.category !== kind.anchor) break; // the other block begins
        if (kind.members.has(f.category) || ROW_OWN_DETAIL.has(f.category)) members.push(f);
        else if (f.category !== "unknown") break; // a question past the block
      }
      for (const f of members) {
        if (ROW_OWN_DETAIL.has(f.category)) {
          f.proposedValue = null;
          f.deviceAbstained = true;
          delete f.deterministic;
          continue;
        }
        if (row === undefined) {
          // More rows asked than the profile has: blank, never another row's.
          f.proposedValue = null;
          delete f.deterministic;
          continue;
        }
        f.groupIndex = row;
        reresolveRowField(f, registry, profile, adapter, fillEEO);
      }
    });
  }
}

/** A field resolved again for the row it now belongs to. */
function reresolveRowField(
  f: DetectedField,
  registry: Map<string, RuntimeControl>,
  profile: UserApplicationProfile,
  adapter: SiteAdapter | null,
  fillEEO: boolean
): void {
  const control = registry.get(f.id);
  const el = control?.el ?? control?.radios?.[0] ?? control?.checkboxes?.[0];
  if (!el) return;
  const options = everyOption(el, f.controlType, f.options);
  const resolved = resolveField({
    adapter,
    category: f.category,
    sensitive: f.sensitive,
    profile,
    control: { controlType: f.controlType, options, groupIndex: f.groupIndex ?? null, multi: control?.multi },
    fillEEO,
    el,
    label: f.label,
    signals: collectSignals(el),
  });
  let value = resolved.value;
  if (f.controlType === "checkbox" && !resolved.deviceAbstained) value = resolveCheckboxIntent(`${f.label} ${f.helpText ?? ""}`, value);
  f.proposedValue = value;
  delete f.deterministic;
  delete f.deviceAbstained;
  Object.assign(f, resolutionFlags(resolved));
}

/**
 * Remap repeating-section rows to 0-based POSITIONAL indices and re-resolve.
 * Workday numbers work-experience rows with an arbitrary instance id
 * ("workExperience-8--jobTitle"), so a field parsed as groupIndex 8 would read
 * profile.experience[8] (undefined) and never fill. Ranking the distinct raw
 * indices maps 8 → 0 and {8,12} → {0,1}, while a standard experience[0]/[1] form
 * is left unchanged.
 */
function remapRepeatingRows(
  fields: DetectedField[],
  registry: Map<string, RuntimeControl>,
  profile: UserApplicationProfile | null,
  adapter: SiteAdapter | null,
  fillEEO: boolean
): void {
  if (!profile) return;
  for (const cats of REPEAT_CATEGORIES) {
    const rowFields = fields.filter((f) => cats.has(f.category) && f.groupIndex != null);
    if (rowFields.length === 0) continue;
    const distinct = [...new Set(rowFields.map((f) => f.groupIndex as number))].sort((a, b) => a - b);
    if (distinct.length === 1 && distinct[0] === 0) continue; // already positional
    const posOf = new Map(distinct.map((raw, i) => [raw, i] as const));
    for (const f of rowFields) {
      const pos = posOf.get(f.groupIndex as number) ?? 0;
      if (pos === f.groupIndex) continue;
      f.groupIndex = pos;
      const control = registry.get(f.id);
      if (!control?.el) continue;
      // Every option, not the panel's capped copy (see scanPage).
      const allOptions = everyOption(control.el, f.controlType, f.options);
      f.proposedValue = guardConstrainedOption(
        resolveAnswerWithAdapter(
          adapter,
          f.category,
          profile,
          { controlType: f.controlType, options: allOptions, groupIndex: pos },
          fillEEO,
          control.el
        ),
        f.controlType,
        allOptions
      );
    }
  }
}

const BARE_ADDRESS = /^[\W\d]*(home |mailing |street |current |residential |permanent )?address( line)?( ?1)?[\W]*$/i;

/** With an Address Line 2 holding the unit ("app. 3"), line 1 drops it:
 *  "4520 rue Saint-Denis", not the unit twice. */
function splitStreetUnit(fields: DetectedField[]): void {
  const units = fields.filter(
    (f) => f.category === "addressStreet" && f.proposedValue && /^(apt|app|appt|apartment|unit|suite|ste|bureau|#)\b/i.test(f.proposedValue)
  );
  if (units.length !== 1) return;
  const suffix = `, ${units[0].proposedValue}`;
  for (const f of fields) {
    if (f !== units[0] && f.category === "addressStreet" && f.proposedValue?.endsWith(suffix)) {
      f.proposedValue = f.proposedValue.slice(0, -suffix.length);
    }
  }
}

/**
 * "Address" alone classifies as the generic `location` (a one-line "where do
 * you live"), which is right on a form with no other location fields. On a form
 * that ALSO asks City or Postal code separately it means the street line:
 * BambooHR got "Toronto, ON, Canada" in Address and City both (live,
 * 2026-10-03). Re-resolved as addressStreet, which fills only a real street.
 */
function reclassifyBareAddress(
  fields: DetectedField[],
  registry: Map<string, RuntimeControl>,
  profile: UserApplicationProfile | null,
  adapter: SiteAdapter | null,
  fillEEO: boolean
): void {
  const hasParts = fields.some((f) => f.category === "addressCity" || f.category === "postalCode");
  if (!hasParts) return;
  for (const f of fields) {
    if (f.category !== "location" || !BARE_ADDRESS.test(f.label.trim())) continue;
    const el = registry.get(f.id)?.el;
    if (!el) continue;
    f.category = "addressStreet";
    const resolved = resolveField({
      adapter,
      category: "addressStreet",
      sensitive: false,
      profile,
      control: { controlType: f.controlType, options: f.options, groupIndex: f.groupIndex ?? null },
      fillEEO,
      el,
      label: f.label,
      signals: collectSignals(el),
    });
    f.proposedValue = resolved.value;
  }
}

/** Inside an EDUCATION block (class / id / automation-id), and not first inside
 *  an employment one. A wrapper naming both says nothing: Greenhouse's older
 *  boards put EMPLOYMENT rows in `.education-experience-block` (Lyft). */
function inEducationBlock(el: HTMLElement): boolean {
  for (let a = el.parentElement, i = 0; a && i < 8; a = a.parentElement, i++) {
    const marks = `${a.id} ${typeof a.className === "string" ? a.className : ""} ${a.getAttribute("data-automation-id") ?? ""}`;
    const edu = /education/i.test(marks);
    const work = /employment|experience|work-?history/i.test(marks);
    if (edu !== work) return edu;
  }
  return false;
}

/**
 * A job still running has no end date. Its "I currently work here" box says
 * so, and a date control's parts take no "Present": Workday removes its "To"
 * date once the box is ticked, and the planned "Present" was reported as two
 * failed fields on every application with a current job (Workday replica,
 * 2026-10-05). A choice control that offers "Present" keeps it, and so does a
 * free-text box.
 */
function dropRunningJobEndDates(fields: DetectedField[], registry: Map<string, RuntimeControl>): void {
  for (const f of fields) {
    if (f.category !== "experienceEndDate" || !f.proposedValue || !PRESENT_RE.test(f.proposedValue)) continue;
    const el = registry.get(f.id)?.el ?? null;
    if (!el) continue;
    const offered = (f.options ?? []).some((o) => PRESENT_RE.test(o));
    const datePart =
      el.matches(DATE_PART_SELECTOR) ||
      /^(date|month|number)$/i.test(el.getAttribute("type") ?? "") ||
      /^(month|year|mm|yyyy|yy)\W*$/i.test((f.label || "").trim());
    const choice = f.controlType === "select" || f.controlType === "combobox" || f.controlType === "customDropdown";
    if ((datePart || choice) && !offered) {
      f.proposedValue = null;
      f.deviceAbstained = true;
    }
  }
}

/**
 * A row date inside an EDUCATION block is the school's date, not a job's.
 * Greenhouse's education block has its own "Start date month / year" and "End
 * date month / year" (`.education--date-container`, ids `start-month--0`): they
 * classify as experience dates and were filled with the first JOB's dates
 * (Twitch, Astranis, live 2026-10-03). The end date is the graduation, split
 * into the month and year controls the way Greenhouse renders them (a bare
 * year leaves the month blank; the expected graduation month fills it); the
 * profile has no education start date, so a start date gets nothing. Ashby's
 * month and year selects carry no name of their own: their options tell them
 * apart. "Still Student?" is ticked for a degree still in progress.
 */
function reclassifyEducationRowDates(
  fields: DetectedField[],
  registry: Map<string, RuntimeControl>,
  profile: UserApplicationProfile | null
): void {
  for (const f of fields) {
    const control = registry.get(f.id);
    const el = control?.el ?? control?.checkboxes?.[0] ?? null;
    if (f.controlType === "checkbox" && el && profile && STILL_STUDENT.test(f.label) && inEducationBlock(el)) {
      const entry = educationRowFacts(profile, f.groupIndex);
      f.proposedValue = entry?.completed === false ? "yes" : entry?.completed === true ? "no" : null;
      // Computed from the profile, so selected on its own evidence: the label
      // classifies weakly, and the box was never filled (Ramp, live 2026-10-03).
      if (f.proposedValue !== null) f.deterministic = true;
      continue;
    }
    if (f.category !== "experienceStartDate" && f.category !== "experienceEndDate") continue;
    if (!el || !inEducationBlock(el)) continue;
    const isEnd = f.category === "experienceEndDate";
    f.category = isEnd ? "graduationYear" : "unknown";
    f.proposedValue = null;
    if (!isEnd || !profile) continue;
    // A row still in progress with its own "Still Student?" box: the box is
    // ticked and the page DISABLES the end date (Ashby; a year written first
    // was left disabled beside a month the page picked: "October 2027", live
    // 2026-10-03). The end date stays the page's, and away from the AI.
    if (educationRowFacts(profile, f.groupIndex)?.completed === false && hasStillStudentBox(el)) {
      f.deviceAbstained = true;
      continue;
    }
    const grad = splitGreenhouseDate(graduationOfRow(profile, educationRowIndex(profile, f.groupIndex)));
    if (!grad) continue;
    // Ashby keeps ONE date per row: choosing only the year set the month to
    // today's ("October 2017"), only the month today's year (Superhuman, live
    // 2026-10-03). A part the profile lacks would be the page's invention, so
    // neither part is written, nor asked of the AI.
    if ((!grad.month || !grad.year) && el.closest(".ashby-application-form-input-dropdown")) {
      f.deviceAbstained = true;
      continue;
    }
    const key = `${el.id} ${el.getAttribute("name") ?? ""} ${f.label}`.toLowerCase();
    const part = /month/.test(key) ? "month" : /year/.test(key) ? "year" : datePartOfOptions(f.options);
    const value = part === "month" ? grad.month : part === "year" ? grad.year : "";
    if (!value) continue;
    f.proposedValue = f.options && f.options.length ? snapToOption(f.options, value, "graduationYear") : value;
  }
}

/** The profile education row a page row means: its own index, or for an
 *  unnumbered row the one its School and Degree resolve to (the main
 *  education: in progress, else the most recent), not merely the first listed.
 *  Superhuman's lone row showed Concordia (2023) beside the first row's 2017. */
function educationRowIndex(profile: UserApplicationProfile, groupIndex: number | null | undefined): number {
  if (groupIndex != null) return groupIndex;
  const primary = profileFacts(profile).education.primary;
  const rows = profile.education ?? [];
  const i = primary ? rows.findIndex((e) => (e.school || "").trim() === primary.school && (e.degree || "").trim() === primary.degree) : -1;
  return i >= 0 ? i : 0;
}

function educationRowFacts(profile: UserApplicationProfile, groupIndex: number | null | undefined): EducationEntryFacts | undefined {
  const row = profile.education?.[educationRowIndex(profile, groupIndex)];
  if (!row) return undefined;
  return profileFacts(profile).education.entries.find((e) => e.school === (row.school || "").trim() && e.degree === (row.degree || "").trim());
}

/** "Still Student?", "Currently attending": the education row is in progress. */
const STILL_STUDENT = /\bstill (a )?student\b|\bcurrent(ly)? (a )?student\b|\bcurrently (attending|enrolled|studying)\b|\bin progress\b/i;

/** The education row around `el` has a "Still Student?" checkbox of its own. */
function hasStillStudentBox(el: HTMLElement): boolean {
  for (let a = el.parentElement, i = 0; a && i < 6; a = a.parentElement, i++) {
    for (const box of Array.from(a.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))) {
      const label = box.closest("label")?.textContent ?? (box.labels?.[0]?.textContent ?? "");
      if (STILL_STUDENT.test(label)) return true;
    }
  }
  return false;
}

const MONTH_OPTION = /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?$/i;
/** A select that names no part: twelve months, or a run of years. */
function datePartOfOptions(options: string[] | undefined): "month" | "year" | "" {
  const opts = (options ?? []).map((o) => o.trim()).filter(Boolean);
  if (opts.filter((o) => MONTH_OPTION.test(o)).length >= 12) return "month";
  if (opts.filter((o) => /^(19|20)\d{2}$/.test(o)).length >= 3) return "year";
  return "";
}

/**
 * A field resolved again now that its widget's REAL options are known. A
 * react-select mounts its list only when opened, so at scan time a question
 * whose answer depends on the options ("Yes, I'd relocate prior to the start"
 * vs "Yes, I'm currently located here"; which channel "How did you hear" offers)
 * can only propose a generic value. With the options it resolves on device, to
 * an exact option, instead of waiting for the AI (Brex, live 2026-10-03).
 */
export function resolveWithOptions(
  field: DetectedField,
  registry: Map<string, RuntimeControl>,
  profile: UserApplicationProfile | null,
  adapter: SiteAdapter | null,
  fillEEO: boolean,
  options: string[]
): string | null {
  const control = registry.get(field.id);
  const el = control?.el ?? control?.radios?.[0] ?? control?.checkboxes?.[0];
  if (!el || !profile || options.length === 0) return null;
  // A self-ID topic is the options' once they load, as the scan reads a list
  // it can see (decision 90): Bracebridge's "Voluntary Equal Employment
  // Opportunity Questionnaire:" over Male | Female | … loaded only when opened
  // and took the decline for a stated Female (Greenhouse, live, round 8). Only
  // between self-ID topics: a field the scan did not hold sensitive stays as
  // it was read.
  const topic = field.sensitive ? withOptionsEvidence({ category: field.category, confidence: field.confidence, sensitive: true }, options, field.label) : null;
  const category = topic?.sensitive ? topic.category : field.category;
  const resolved = resolveField({
    adapter,
    category,
    sensitive: field.sensitive,
    profile,
    control: { controlType: field.controlType, options, groupIndex: field.groupIndex ?? null, multi: control?.multi },
    fillEEO,
    el,
    label: field.label,
    signals: collectSignals(el),
  });
  return resolved.value;
}

export function scanPage(
  profile: UserApplicationProfile | null,
  fillEEO: boolean,
  adapter: SiteAdapter | null = getAdapter(location.hostname, location.href)
): ScanResult {
  const fields: DetectedField[] = [];
  const registry = new Map<string, RuntimeControl>();
  assignedThisScan.clear();

  const candidates = deepQueryAll(document, CANDIDATE_SELECTOR);
  const radioGroups = new Map<string, HTMLInputElement[]>();
  const nameless = namelessRadioKeys();
  const checkboxGroups = new Map<Element, HTMLInputElement[]>();

  for (const el of candidates) {
    // A same-origin child page's controls are its own content script's: it
    // scans them and announces its form. Its ARIA widgets pass the checks
    // below (its native inputs fail instanceof), so on iCIMS's login (live
    // 2026-10-17) the top frame took half of the frame's form for its own,
    // kept the flow, and the frame was never adopted. A blank frame (an
    // editor) has no script of its own and stays the top frame's to read.
    if (el.ownerDocument !== document && /^https?:/i.test(el.ownerDocument.URL)) continue;
    const controlType = controlTypeOf(el);
    if (controlType === null) continue;
    // A role=radiogroup that only WRAPS native radios (Material UI renders two
    // such wrappers per question) is presentation: the native group is the
    // control. Scanned as an ARIA group it had no options and logged a failed
    // fill beside the native group that filled (BambooHR, live 2026-10-03).
    // So is one whose every role=radio WRAPS a native radio (Workable's Yes /
    // No, live 2026-10-05: the ARIA twin read no options and reported "No
    // option matches" beside the native group it duplicated, which filled).
    if (controlType === "ariaRadioGroup" && el.querySelector('input[type="radio"]')) {
      const ariaRadios = Array.from(el.querySelectorAll('[role="radio"]'));
      if (ariaRadios.length === 0 || ariaRadios.every((r) => r.querySelector('input[type="radio"]'))) continue;
    }
    // Never surface or fill a captcha widget's own controls, fill around it.
    if (isCaptchaField(el)) continue;
    // Skip cookie-consent / privacy-banner controls. They are real form
    // controls but never application fields; counting them leaves the panel
    // stuck on a consent dialog when the real form is lazy-mounted.
    if (isConsentField(el)) continue;
    // Page chrome (header/nav/footer/aside and landmark roles) is never part
    // of the application form, an EN/FR switcher is a real <select> we skip.
    if (isInPageChrome(el)) continue;
    // A site's own job search (keyword box, sort, results per page) is not
    // the application's either (siteSearch.ts).
    if (isSiteSearch(el)) continue;
    if ((el as HTMLInputElement).disabled) continue;
    // A read-only box is the page's, except a dropdown's: Ant Design's select
    // without a search box keeps a read-only input as its focus target
    // (Dayforce's required "How did you hear about this job?", live
    // 2026-10-05, was never seen).
    if (el instanceof HTMLInputElement && el.readOnly && !(controlType === "combobox" && opensList(el))) continue;
    // select2 / chosen: the visible widget is a proxy of a hidden native
    // <select> that holds the real options and value. Fill the select (the
    // library follows its change event) and never the proxy or its dropdown.
    if (isEnhancedSelectProxy(el)) continue;
    // A dropdown's own search box is no field of its own: Paylocity's select
    // is a box with aria-haspopup="listbox" around a typeahead input, and the
    // input, scanned too, got "TX" typed in and failed beside the dropdown
    // that took "Texas" (live 2026-10-05). The box drives it.
    if (controlType === "text" && el instanceof HTMLInputElement && el.parentElement?.closest('[aria-haspopup="listbox"]')) continue;
    const enhancedSelect = isEnhancedSelect(el);

    // Visibility: checkbox/radio/file are often visually hidden behind styled
    // replacements but still operable, allow them when labeled. Comboboxes get
    // NO relaxation: an invisible combobox is not user-operable (react-select's
    // real input is small but rendered; what hides fully is other widgets'
    // internals, e.g. intl-tel-input's dial-code search inside a closed dialog).
    const relaxed =
      controlType === "checkbox" ||
      controlType === "radioGroup" ||
      controlType === "file";
    if (
      !isVisible(el) &&
      !enhancedSelect &&
      !(relaxed && (isHiddenButLabeled(el) || isUploadAffordance(el)) && hasRenderedProxy(el))
    ) {
      continue;
    }
    // A control inside aria-hidden markup is by definition not part of the form
    // the user sees (react-select's `<input required>` validation twin, screen-
    // reader-excluded duplicates). Styled-replacement natives (checkbox/radio/
    // file) legitimately carry aria-hidden, so only strict types are skipped.
    // An open menu's "hide others" mark is not hiding (hiddenFromReaders).
    if (!relaxed && !enhancedSelect && hiddenFromReaders(el)) continue;

    if (el instanceof HTMLInputElement && el.type === "radio") {
      // A name ties a radio group together in the browser; a framework
      // (Vue v-model, hand-rolled React) may tie it together instead and
      // render no name at all. Those group by their question container.
      const groupKey = el.name
        ? `${el.form?.id ?? "noform"}::${el.name}`
        : `container::${nameless.keyFor(el) ?? ensureFieldId(el)}`;
      const group = radioGroups.get(groupKey) ?? [];
      group.push(el);
      radioGroups.set(groupKey, group);
      continue; // grouped below
    }

    // "Select all that apply": checkboxes sharing a natural group container
    // (fieldset/[role=group], or the closest plain-<div> ancestor enclosing
    // ≥2 of them and nothing else) are one multi-select field. A standalone
    // checkbox (no such container, or only one inside it) falls through to
    // the single-control path.
    if (el instanceof HTMLInputElement && el.type === "checkbox") {
      // Ashby's Yes/No keeps a hidden checkbox under its buttons: the group is
      // the control (PRESSED_GROUP), the checkbox only its form value.
      if (el.closest(PRESSED_GROUP)) continue;
      const container = checkboxGroupContainer(el);
      if (container) {
        const group = checkboxGroups.get(container) ?? [];
        group.push(el);
        checkboxGroups.set(container, group);
        continue; // emitted as one checkboxGroup below
      }
    }

    const id = ensureFieldId(el);
    const signals = collectSignals(el);

    // Passwords: registry-tracked for the account sub-flow, but never listed
    // as a generic field, never fillable generically, never sent to the AI,
    // and the value is never echoed into the serializable field.
    if (controlType === "password") {
      registry.set(id, { id, controlType, el });
      fields.push({
        id,
        category: "accountPassword",
        confidence: 1,
        label: bestDisplayLabel(signals),
        controlType,
        required: isRequiredField(el, signals),
        proposedValue: null,
        fillable: false,
        sensitive: false,
        note: "Handled by the account sign-up flow.",
        currentValue: (el as HTMLInputElement).value ? "filled" : undefined,
      });
      continue;
    }

    const groupIndex = detectGroupIndex(signals);
    let { category, confidence, sensitive } = choiceIsNoUpload(classifyWithAdapter(adapter, { el, signals, controlType }), controlType);

    const options =
      el instanceof HTMLSelectElement
        ? selectOptions(el)
        : controlType === "combobox"
          ? readComboboxOptions(el)
          : controlType === "ariaRadioGroup"
            ? ariaRadioOptions(el)
            : undefined;
    // The options' own evidence too, as for radio and checkbox groups: SRC's
    // VEVRAA notice is a SELECT of two veteran options (Jobvite bank 2026-10-17).
    if (options?.length) ({ category, confidence, sensitive } = withOptionsEvidence({ category, confidence, sensitive }, options, signals.label));
    // A dialing-code picker is the phone's code: Rippling's combobox labelled
    // "Search" showing "+44 GB" (live 2026-10-05), or a list of bare "+49"
    // codes, which the label beside it made the phone number itself.
    if ((category === "unknown" || category === "phone") && isDialPicker(el) && blockPartner(el, isPhoneInput)) {
      ({ category, confidence, sensitive } = { category: "phoneCountryCode", confidence: 0.95, sensitive: false });
    }

    const driver =
      controlType === "combobox" || controlType === "customDropdown"
        ? detectFillDriver(el) ?? undefined
        : undefined;
    // A skills combobox is multi-value ("Type to Add Skills"): the engine adds
    // one chip per skill rather than matching the joined list as one option.
    // So is a react-select multi (its value container "--is-multi"): Gallup's
    // "Please indicate which languages you speak…" on Greenhouse was read as a
    // single choice, and the language rule never answered it (live, round 7).
    const multi =
      controlType === "combobox" &&
      (category === "skills" || el.getAttribute("aria-multiselectable") === "true" || el.closest('[class*="value-container--is-multi"]') !== null);
    const control: RuntimeControl = { id, controlType, el, driver, multi };
    registry.set(id, control);

    const label = bestDisplayLabel(signals);
    // Kind → question shapes → category value → kind/option gate (fieldResolver).
    // Resolution sees EVERY option: `options` is capped at 60 for the panel,
    // and a value past the cap ("United States" in a country list) was dropped
    // as "not offered" when the gate used the capped copy.
    const allOptions = everyOption(el, controlType, options);
    const resolved = resolveField({
      adapter,
      category,
      sensitive,
      profile,
      control: { controlType, options: allOptions, groupIndex, multi },
      fillEEO,
      el,
      label,
      signals,
    });
    let proposedValue = resolved.value;
    // Beside its own dialing-code picker, the box takes the national number:
    // typed whole, "+49 30 12345678" kept its digits under the picker's "+44".
    if (category === "phone" && proposedValue) {
      const shownCode = /^\s*\+\s?(\d{1,4})\s*$/.exec((el as HTMLInputElement).value ?? "")?.[1] ?? null;
      if (blockPartner(el, isDialPicker)) proposedValue = nationalNumber(proposedValue);
      // A box that shows its code is in international form (Recruitee's "+1").
      else if (shownCode) proposedValue = internationalNumber(proposedValue, profile?.country ?? null, shownCode);
    }
    // A single checkbox is a boolean control: never write a text value into it.
    // Check clear application consent, skip marketing / ambiguous boxes (→ null,
    // so they're simply not selected rather than counted as failures).
    let consentTick = false;
    if (controlType === "checkbox" && !resolved.deviceAbstained) {
      const text = `${label} ${signals.nearby ?? ""}`;
      proposedValue = resolveCheckboxIntent(text, proposedValue);
      // A consent tick is selected on its own evidence, whatever the label's
      // category score: whether it got ticked used to depend on how confidently
      // its words happened to classify.
      consentTick = proposedValue === "yes" && isConsentText(text);
    }

    fields.push({
      id,
      category,
      confidence,
      label,
      controlType,
      required: isRequiredField(el, signals),
      proposedValue,
      fillable:
        driver !== undefined ||
        (controlType !== "file" && controlType !== "customDropdown"),
      sensitive,
      note: noteFor(controlType, category),
      options,
      helpText: signals.nearby,
      inputType: signals.typeHint,
      groupIndex,
      // A picker showing a bare dialing code shows the page's preset (from
      // the visitor's locale, "+44 GB"), not an answer: left as one, the
      // picker was never selected for the fill. So does a phone box holding
      // only the code ("+1", Recruitee, live 2026-10-05: never filled). So
      // does a box still showing what the page guessed from the visitor's
      // location (Workable's Address, pageGuesses.ts).
      currentValue:
        (category === "phoneCountryCode" && isDialPicker(el)) ||
        (category === "phone" && /^\s*\+\s?\d{1,4}\s*$/.test((el as HTMLInputElement).value ?? "")) ||
        holdsPageGuess(el)
          ? undefined
          : currentValueOf(el, controlType),
      ...resolutionFlags(resolved),
      ...(consentTick ? { deterministic: true } : {}),
    });
  }

  // Radio groups become a single logical field each.
  for (const radios of radioGroups.values()) {
    const first = radios[0];
    const id = ensureFieldId(first);
    const signals = groupSignals(radios, first.closest('fieldset, [role="radiogroup"]'));
    const groupIndex = detectGroupIndex(signals);
    const options = radios.map(radioOptionLabel).filter(Boolean).slice(0, MAX_GROUP_OPTIONS);
    let { category, confidence, sensitive } = choiceIsNoUpload(classifyWithAdapter(adapter, { el: first, signals, controlType: "radioGroup" }), "radioGroup");
    ({ category, confidence, sensitive } = withOptionsEvidence({ category, confidence, sensitive }, options, signals.label));

    registry.set(id, { id, controlType: "radioGroup", radios });

    const label = bestDisplayLabel(signals);
    const resolved = resolveField({
      adapter,
      category,
      sensitive,
      profile,
      control: { controlType: "radioGroup", options, groupIndex },
      fillEEO,
      el: first,
      label,
      signals,
    });

    const checked = radios.find((r) => r.checked);
    fields.push({
      id,
      category,
      confidence,
      label,
      controlType: "radioGroup",
      required: radios.some((r) => isRequiredField(r, signals)),
      proposedValue: resolved.value,
      fillable: true,
      sensitive,
      note: noteFor("radioGroup", category),
      options,
      helpText: signals.nearby,
      inputType: signals.typeHint,
      groupIndex,
      currentValue: checked ? radioOptionLabel(checked) : undefined,
      ...resolutionFlags(resolved),
    });
  }

  // Native checkbox groups ("select all that apply"), one logical multi-select
  // field each, classified by the group question (not the option text).
  for (const [container, checkboxes] of checkboxGroups.entries()) {
    const first = checkboxes[0];
    const id = ensureFieldId(first);
    const signals = groupSignals(checkboxes, container);
    const groupIndex = detectGroupIndex(signals);
    const options = checkboxes.map(radioOptionLabel).filter(Boolean).slice(0, MAX_GROUP_OPTIONS);
    let { category, confidence, sensitive } = choiceIsNoUpload(classifyWithAdapter(adapter, { el: first, signals, controlType: "checkboxGroup" }), "checkboxGroup");
    // As for radios: Workday's disability form asks only "Please check one of
    // the boxes below:" over three disability boxes, and stayed unanswered.
    ({ category, confidence, sensitive } = withOptionsEvidence({ category, confidence, sensitive }, options, signals.label));

    registry.set(id, { id, controlType: "checkboxGroup", checkboxes });

    const label = bestDisplayLabel(signals);
    const resolved = resolveField({
      adapter,
      category,
      sensitive,
      profile,
      control: { controlType: "checkboxGroup", options, groupIndex },
      fillEEO,
      el: first,
      label,
      signals,
    });

    const checkedLabels = checkboxes.filter((c) => c.checked).map(radioOptionLabel).filter(Boolean);
    fields.push({
      id,
      category,
      confidence,
      label,
      controlType: "checkboxGroup",
      required: checkboxes.some((c) => isRequiredField(c, signals)),
      proposedValue: resolved.value,
      fillable: true,
      sensitive,
      note: noteFor("checkboxGroup", category),
      options,
      helpText: signals.nearby,
      inputType: signals.typeHint,
      groupIndex,
      currentValue: checkedLabels.length ? checkedLabels.join(", ") : undefined,
      ...resolutionFlags(resolved),
    });
  }

  // Custom (non-<input>) résumé / cover-letter upload widgets (SuccessFactors).
  scanCustomUploads(fields, registry);

  // A bare "Address" next to separate City / Postal fields is the street line.
  reclassifyBareAddress(fields, registry, profile, adapter, fillEEO);
  splitStreetUnit(fields);
  // Rows repeated with the same ids (Ashby) → indices by position.
  assignRowsByPosition(fields, registry, profile, adapter, fillEEO);
  // Flat questionnaires asking for a school or employer more than once.
  assignQuestionnaireRows(fields, registry, profile, adapter, fillEEO);
  reclassifyEducationRowDates(fields, registry, profile);

  // Repeating-section rows → positional indices (Workday's instance-numbered rows).
  remapRepeatingRows(fields, registry, profile, adapter, fillEEO);
  dropRunningJobEndDates(fields, registry);

  // Scope to the application-form container; anything outside is noise even
  // when its category is known. No qualifying container → unscoped fallback.
  const entries: ScopeEntry[] = fields.flatMap((f) => {
    const c = registry.get(f.id);
    const el = c?.el ?? c?.radios?.[0] ?? c?.checkboxes?.[0];
    return el ? [{ field: f, el }] : [];
  });
  const scopeEl = resolveFormScope(entries);
  if (!scopeEl) return { fields, registry, adapter, scopeEl: null };
  const keep = new Set(filterToScope(entries, scopeEl).map((e) => e.field.id));
  const scoped = fields.filter((f) => keep.has(f.id));
  for (const f of fields) if (!keep.has(f.id)) registry.delete(f.id);
  return { fields: scoped, registry, adapter, scopeEl };
}

/**
 * Workday's segmented date widget renders each EMPTY part as a spinbutton
 * reading "0". Read that as empty, or the part looks already-filled and every
 * path that acts on a blank field skips it: aiFillPlanner's `!f.currentValue`,
 * answerGaps' currentValue guard, and shared/selection's default selection.
 *
 * TWO independent signals, because neither covers the other's ground:
 *
 *  - a `dateSection*` automation-id (DATE_PART_ID_SELECTOR). 0 is never a
 *    month, a day or a year, so this is sound BY CONSTRUCTION. It rests on no
 *    attribute a tenant may or may not emit.
 *  - `aria-valuemin` above zero on a role=spinbutton, for a part whose
 *    automation-id a tenant has renamed out from under us.
 *
 * `aria-valuemin` alone was one unverified attribute away from being a no-op:
 * it was inferred from a bug report, never captured from a live tenant, and a
 * part rendered with no `aria-valuemin`: or with `aria-valuemin="0"`: fell
 * straight back through as "already filled" (see workdayDateParts.test.ts).
 *
 * An ordinary <input type="number"> where 0 IS the answer ("years of
 * experience: 0") carries neither signal and is untouched.
 */
function readsEmptyAsZero(el: HTMLElement, raw: string): boolean {
  if (raw.trim() !== "0") return false;
  if (el.matches(DATE_PART_ID_SELECTOR)) return true;
  if (el.getAttribute("role") !== "spinbutton") return false;
  const min = Number(el.getAttribute("aria-valuemin"));
  return Number.isFinite(min) && min > 0;
}

/** A select's "nothing chosen yet" entry that carries a value of its own. */
const SELECT_PLACEHOLDER = /^[-–—\s]*(no (answer|selection|response)|none selected|not selected|nothing selected)[-–—\s]*$/i;

/**
 * A hidden native behind a styled replacement is operable only when the
 * replacement is on screen, and nothing around it renders when an ancestor is
 * hidden: a whole unopened tab (Recruitee's "Apply", live 2026-10-05, whose
 * radios made the flow fill a form nobody could see). The native itself may
 * be hidden any way it likes.
 */
function hasRenderedProxy(el: HTMLElement): boolean {
  for (let node = el.parentElement; node; node = node.parentElement) {
    if (node.hidden) return false;
    const style = getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
  }
  return true;
}

/** A select or combobox holding dialing codes ("+44 GB", "+49"). A combobox
 *  whose list is in the page is read by its options too: iCIMS's "Country
 *  Code" is a link over "(+93) Afghanistan"… (Schwab, live 2026-10-17). */
function isDialPicker(el: HTMLElement): boolean {
  if (el instanceof HTMLSelectElement) {
    const options = Array.from(el.options, (o) => cleanText(o.textContent));
    return looksLikeDialCodes(cleanText(el.selectedOptions[0]?.textContent), options);
  }
  if (el.getAttribute("role") !== "combobox") return false;
  if (el instanceof HTMLInputElement && looksLikeDialCodes(el.value, undefined)) return true;
  return looksLikeDialCodes(cleanText(el.textContent), readComboboxOptions(el, Infinity));
}

const PHONE_WORDS = /\b(phone|mobile|telephone|tel|cell)\b/i;

/** A text box for a phone number, by its type, input mode or names. */
function isPhoneInput(el: HTMLElement): boolean {
  if (!(el instanceof HTMLInputElement) || el.getAttribute("role") === "combobox") return false;
  if (el.type === "tel" || el.getAttribute("inputmode") === "tel" || /^tel/.test(el.autocomplete)) return true;
  const labelledBy = (el.getAttribute("aria-labelledby") ?? "")
    .split(/\s+/)
    .map((id) => (id ? el.ownerDocument.getElementById(id)?.textContent ?? "" : ""))
    .join(" ");
  const names = [el.name, el.id, el.placeholder, el.getAttribute("aria-label") ?? "", labelledBy].join(" ");
  return PHONE_WORDS.test(names.replace(/[_-]+/g, " "));
}

/**
 * The control that shares a small block with `el` and passes `want`: the
 * nearest ancestor holding at most three controls (a picker, its number and
 * an extension), never a wider section of the form. Only what shows counts
 * toward the three: iCIMS keeps two hidden boxes behind its code picker
 * (Schwab, live 2026-10-17). An ARIA combobox is a control too.
 */
function blockPartner(el: HTMLElement, want: (c: HTMLElement) => boolean): HTMLElement | null {
  let node = el.parentElement;
  for (let depth = 0; depth < 12 && node; depth++, node = node.parentElement) {
    const controls = Array.from(
      node.querySelectorAll<HTMLElement>('input:not([type="hidden"]):not([type="file"]), select, textarea, [role="combobox"]')
    );
    if (controls.filter((c) => isVisible(c)).length > 3) return null;
    const hit = controls.find((c) => c !== el && want(c));
    if (hit) return hit;
  }
  return null;
}

function currentValueOf(el: HTMLElement, controlType: ControlType): string | undefined {
  if (controlType === "select") {
    const sel = el as HTMLSelectElement;
    const opt = sel.selectedOptions[0];
    // Treat a selected placeholder ("Select…", empty value) as empty.
    if (!opt || !opt.value) return undefined;
    // …and one that carries a value: JazzHR's "No answer" (value "0") and
    // "-- No answer --" ("resumator_no_selection") read as answers already
    // given, and ten questions were never filled (live 2026-10-05).
    if (SELECT_PLACEHOLDER.test(cleanText(opt.textContent)) || isPlaceholderFiller(cleanText(opt.textContent))) return undefined;
    // A decline the PAGE ships selected ("Decline to answer" with `selected`,
    // JazzHR's EEO selects, live 2026-10-03) is its default, not an answer:
    // the applicant's stated one may replace it. Any other default stays.
    if (opt.defaultSelected && isDeclineText(opt.textContent ?? "")) return undefined;
    return cleanText(opt.textContent) || undefined;
  }
  if (controlType === "checkbox") {
    return (el as HTMLInputElement).checked ? "checked" : undefined;
  }
  if (controlType === "text" || controlType === "textarea") {
    const v = (el as HTMLInputElement | HTMLTextAreaElement).value;
    if (!v) return undefined;
    return readsEmptyAsZero(el, v) ? undefined : v;
  }
  if (controlType === "contenteditable") {
    const v = cleanText(el.textContent);
    return v ? v : undefined;
  }
  if (controlType === "combobox") {
    return readComboboxValue(el);
  }
  if (controlType === "ariaRadioGroup") {
    const checked = slottedQueryAll(el, '[role="radio"][aria-checked="true"], button[aria-pressed="true"]')[0];
    if (!checked) return undefined;
    return (cleanText(checked.getAttribute("aria-label")) || cleanText(checked.getAttribute("label")) || cleanText(checked.textContent)) || undefined;
  }
  return undefined;
}

function noteFor(controlType: ControlType, category: string): string | undefined {
  if (controlType === "file") {
    return category === "resumeUpload"
      ? "Browser security requires choosing the file manually. Click the field and pick your resume."
      : "File uploads must be selected manually.";
  }
  if (controlType === "customDropdown") {
    return "Custom dropdown. Please select manually.";
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Dynamic page support
// ---------------------------------------------------------------------------

const OBSERVE_OPTS: MutationObserverInit = { childList: true, subtree: true };

/**
 * Every open shadow root reachable from `root` (nested included). SuccessFactors-
 * style UI5 fields live in open shadow roots, which are the SAME JS realm as the top
 * document, so the scanner already classifies them, but a top-documentElement
 * MutationObserver never sees mutations inside them. Same-origin iframes are NOT
 * included: their fields are a different realm the top frame can't classify (they
 * run their own content-script instance), so observing them would only cause
 * pointless rescans.
 */
export function openShadowRoots(root: Document | ShadowRoot): ShadowRoot[] {
  const out: ShadowRoot[] = [];
  const visit = (node: Document | ShadowRoot): void => {
    node.querySelectorAll("*").forEach((el) => {
      // Our own panel is not part of the page: observing it turned every
      // panel repaint into a "page change" and a rescan (see observePage).
      if (EXTENSION_UI_HOST_IDS.has((el as HTMLElement).id)) return;
      const sr = (el as HTMLElement).shadowRoot;
      if (sr) {
        out.push(sr);
        visit(sr);
      }
    });
  };
  visit(root);
  return out;
}

/** A mutation that happened inside (or to) the extension's own UI host. */
function isOwnUiMutation(m: MutationRecord): boolean {
  const target = m.target as Node;
  const root = target.getRootNode?.();
  const host = root instanceof ShadowRoot ? (root.host as HTMLElement) : null;
  if (host && EXTENSION_UI_HOST_IDS.has(host.id)) return true;
  const nodes = [...Array.from(m.addedNodes), ...Array.from(m.removedNodes)];
  return nodes.length > 0 && nodes.every((n) => n instanceof HTMLElement && EXTENSION_UI_HOST_IDS.has(n.id));
}

/**
 * Watch for DOM changes (SPA navigation, multi-step Workday forms, UI5 shadow-DOM
 * steps) and call back, debounced. Observes the top document AND every open shadow
 * root, re-attaching to roots that appear later. Attribute changes are ignored, we
 * cause those ourselves when assigning field ids and flashing highlights.
 */
export function observePage(onChange: () => void): MutationObserver {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const observed = new Set<Node>();
  const attach = (): void => {
    if (!observed.has(document.documentElement)) {
      observed.add(document.documentElement);
      observer.observe(document.documentElement, OBSERVE_OPTS);
    }
    for (const root of openShadowRoots(document)) {
      if (observed.has(root)) continue;
      observed.add(root);
      observer.observe(root, OBSERVE_OPTS);
    }
  };
  const observer = new MutationObserver((mutations) => {
    // Only the PAGE changing is a reason to rescan. The panel repainting itself
    // (job-card logo swap, button label) used to count, and the rescan it
    // caused repainted the panel again: a rescan every 500 ms, forever.
    const relevant = mutations.some(
      (m) => (m.addedNodes.length > 0 || m.removedNodes.length > 0) && !isOwnUiMutation(m)
    );
    if (!relevant) return;
    attach(); // pick up newly-added shadow roots
    if (timer) clearTimeout(timer);
    timer = setTimeout(onChange, 500);
  });
  attach();
  return observer;
}
