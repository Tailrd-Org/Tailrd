/**
 * DOM helpers used by the scanner, matcher and autofill engine.
 * No Chrome APIs in here, pure DOM, easy to unit test later.
 */
import { UNLABELED_FIELD, isMachineId } from "../shared/questionText";
import { isLegacyReactSelect } from "./driverDetect";

/** Collapse whitespace and trim. */
export function cleanText(text: string | null | undefined): string {
  return (text ?? "").replace(SVG_FALLBACK, " ").replace(/\s+/g, " ").trim();
}

/** The text a page puts inside its icons for browsers without SVG: never part
 *  of a label ("SVGs not supported by this browser." three times before "I
 *  currently work here", Workable, live 2026-10-03). */
const SVG_FALLBACK = /\bSVGs? (are )?not supported by this browser\.?/gi;

/**
 * Keep a node attached: if the page (a SPA re-render or client-side navigation)
 * tore `node` out of the document, re-append it to `parent`. Returns true if it
 * had to re-attach. This is what stops the in-page overlay from silently dying
 * on React/Angular sites that rebuild the DOM out from under it.
 */
export function reattachIfDetached(node: HTMLElement, parent: ParentNode): boolean {
  if (node.isConnected) return false;
  parent.appendChild(node);
  return true;
}

/**
 * The extension's own in-page UI host (the side panel). Its shadow root holds
 * real form controls (a cover-letter tone <select>, résumé pickers, EEO inputs)
 * that are NOT page fields, deepQueryAll must never descend into it, or the
 * scanner counts our own UI as the page's form and a bare job posting reads as a
 * filled form (so the flow never clicks Apply).
 */
export const EXTENSION_UI_HOST_IDS = new Set(["applypilot-overlay-host"]);

/**
 * querySelectorAll that also descends into open shadow roots AND same-origin
 * iframes. Several ATS embed their form in an iframe (Greenhouse/Lever boards)
 * or render widgets inside shadow DOM. Cross-origin iframes throw on access and
 * are skipped silently, those frames run their own copy of the content script.
 * The extension's own UI hosts are skipped entirely (see EXTENSION_UI_HOST_IDS).
 */
export function deepQueryAll(root: ParentNode, selector: string): HTMLElement[] {
  const out: HTMLElement[] = [];
  const seen = new Set<Document | ShadowRoot>();
  const visit = (node: ParentNode): void => {
    node.querySelectorAll(selector).forEach((el) => out.push(el as HTMLElement));
    node.querySelectorAll("*").forEach((el) => {
      // Never traverse into our own panel / modal, its controls aren't page
      // fields and must never be scanned, matched, or clicked by the flow.
      if (el instanceof HTMLElement && EXTENSION_UI_HOST_IDS.has(el.id)) return;
      const shadow = (el as HTMLElement).shadowRoot;
      if (shadow) visit(shadow);
      if (el instanceof HTMLIFrameElement) {
        const doc = sameOriginDocument(el);
        if (doc && !seen.has(doc)) {
          seen.add(doc);
          visit(doc);
        }
      }
    });
  };
  visit(root);
  return out;
}

/** A same-origin iframe's document, or null if cross-origin / not ready. */
function sameOriginDocument(iframe: HTMLIFrameElement): Document | null {
  try {
    return iframe.contentDocument;
  } catch {
    return null; // cross-origin, accessing contentDocument throws
  }
}

/** Visible enough to be a real, user-facing field. */
export function isVisible(el: HTMLElement): boolean {
  const style = getComputedStyle(el);
  if (style.display === "none" || style.visibility === "hidden") return false;
  // The sr-only "clip" trick keeps an element in layout while collapsing it to
  // nothing. Bot-trap honeypot inputs (e.g. Workday's `beecatcher`, name=website)
  // hide this way; filling one flags the submission as a bot and the ATS silently
  // refuses to advance. A user never sees it, so we must not either.
  if (isClipHidden(style)) return false;
  // Zero- or sub-pixel-area boxes are invisible companions, not fields: react-select
  // renders a hidden `<input required>` twin (height:0, opacity:0) purely so native
  // form validation fires, and honeypots use a ~1px clipped box, typing into either
  // churns the widget / trips bot detection. A genuine field has a real rendered box.
  return Array.from(el.getClientRects()).some((r) => r.width > 1 && r.height > 1);
}

/**
 * The text an element shows: its text without the parts the page hides
 * (display:none / visibility:hidden), with a space at every box boundary.
 * textContent runs a responsive button's two labels together: iCIMS's
 * "Apply for this job online" keeps a short "Apply" beside it, hidden, and
 * read as "Apply for this job onlineApply" (live 2026-10-17). jsdom has no
 * innerText, so this walks the tree itself.
 */
export function shownText(el: Element): string {
  if (!el.firstElementChild) return cleanText(el.textContent);
  let out = "";
  const walk = (node: Node): void => {
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        out += child.textContent ?? "";
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const style = getComputedStyle(child as Element);
        if (style.display === "none" || style.visibility === "hidden") continue;
        const box = style.display !== "inline" && style.display !== "contents";
        if (box) out += " ";
        walk(child);
        if (box) out += " ";
      }
    }
  };
  walk(el);
  return cleanText(out);
}

/**
 * The "visually hidden" clip trick, used for sr-only content and, critically,
 * for bot-trap honeypot inputs: a `clip: rect(1px,1px,1px,1px)` (or `rect(0,…)`)
 * or a zero-area `clip-path` collapses the box to nothing while leaving it in the
 * layout (so `getClientRects` still reports a ~1px box).
 */
function isClipHidden(style: CSSStyleDeclaration): boolean {
  const clipPath =
    style.clipPath || (style as unknown as { webkitClipPath?: string }).webkitClipPath || "";
  if (clipPath && clipPath !== "none") {
    const cp = clipPath.trim();
    // polygon(0px 0px, 0px 0px, …), every vertex at the origin ⇒ zero area.
    if (/^polygon\((?:\s*0(?:px|%)?\s+0(?:px|%)?\s*,?)+\)$/.test(cp)) return true;
    // inset(50%…)/inset(100%…) collapse the box from the edges inward.
    if (/^inset\(\s*(?:100|[5-9]\d)(?:\.\d+)?%/.test(cp)) return true;
  }
  const clip = style.clip;
  if (clip && clip !== "auto") {
    const nums = clip.match(/-?\d*\.?\d+/g)?.map(Number) ?? [];
    if (nums.length === 4) {
      const [top, right, bottom, left] = nums; // rect(top, right, bottom, left)
      if (right - left <= 1 && bottom - top <= 1) return true;
    }
  }
  return false;
}

/**
 * Inside markup hidden from screen readers. A popup's "hide others" mark is
 * not that: the aria-hidden library sets `aria-hidden` with
 * `data-aria-hidden="true"` on everything outside an open menu and removes
 * both when it closes, and the user still sees every field it marked. While
 * Ashby's school menu was open, a scan read the rest of the form as hidden
 * and lost the second school ("Field no longer found", Superhuman, live
 * 2026-10-05). A mark wrapping markup that is hidden for good still hides it.
 */
export function hiddenFromReaders(node: Element): boolean {
  return node.closest('[aria-hidden="true"]:not([data-aria-hidden="true"])') !== null;
}

/**
 * Many ATS visually hide the native input behind a styled replacement
 * (custom checkboxes, drag-and-drop resume zones). Those are still real,
 * fillable controls as long as something labels them.
 */
export function isHiddenButLabeled(el: HTMLElement): boolean {
  const labels = (el as HTMLInputElement).labels;
  if (labels && labels.length > 0) return true;
  return Boolean(el.getAttribute("aria-label") || el.getAttribute("aria-labelledby"));
}

/** Resolve aria-labelledby into text. Nodes INSIDE the control itself are
 *  skipped: widgets (react-aria, Radix) point aria-labelledby at their own
 *  value/placeholder span, whose text is "Select…", the current selection,
 *  never the question. */
function ariaLabelledByText(el: HTMLElement): string {
  const ids = el.getAttribute("aria-labelledby");
  if (!ids) return "";
  const doc = el.ownerDocument;
  return cleanText(
    ids
      .split(/\s+/)
      .map((id) => {
        const ref = doc.getElementById(id);
        if (!ref || el.contains(ref)) return "";
        return ref.textContent ?? "";
      })
      .join(" ")
  );
}

/** Text of associated <label> elements (covers both for= and wrapping). */
export function associatedLabelText(el: HTMLElement): string {
  const labels = (el as HTMLInputElement).labels;
  if (labels && labels.length > 0) {
    return cleanText(Array.from(labels).map((l) => l.textContent ?? "").join(" "));
  }
  const wrapping = el.closest("label");
  if (wrapping) return cleanText(wrapping.textContent);
  return containerLabelText(el);
}

/**
 * A label whose `for` names a CONTAINER of the control: Ashby's "Start Date"
 * points at a div holding a month select and a year select, neither of which
 * has an id. The year select took the month select's option text as its label
 * ("Month...JanuaryFebruary...", live 2026-10-03).
 */
function containerLabelText(el: HTMLElement): string {
  for (let a = el.parentElement, i = 0; a && i < 3; a = a.parentElement, i++) {
    if (!a.id || /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(a.tagName)) continue;
    const id = a.id;
    const label = Array.from(a.ownerDocument.getElementsByTagName("label")).find((l) => l.htmlFor === id);
    if (label) return cleanText(label.textContent);
  }
  return "";
}

/**
 * Placeholder / "no selection yet" filler that a dropdown shows before the user
 * picks, e.g. react-select's `<div class="select__placeholder">Select…</div>`.
 * This is NEVER the field's question, but it commonly sits as a sibling of the
 * inner combobox input, so nearbyText would otherwise grab it as the label (and
 * the classifier would then see "select" instead of "Country"/"Gender"). We
 * match the WHOLE trimmed string so a real label like "Select your country"
 * still counts as a label.
 */
const PLACEHOLDER_FILLER =
  /^[-\s]*(please\s+)?(select|choose|pick)( (an?|one|your) )?( ?(option|value|answer|choice|one|country|item))?\s*(\.{3}|…)?[-\s]*$/i;

export function isPlaceholderFiller(text: string): boolean {
  return PLACEHOLDER_FILLER.test(text.trim());
}

/**
 * Fallback when there is no <label>: walk previous siblings (including bare
 * text nodes), then climb a few ancestors and repeat. This catches the very
 * common ATS markup `<div><span>Label</span><input/></div>`. Dropdown
 * placeholder filler ("Select…") is skipped so it never masquerades as a label.
 */
/** `node` is a <label>/<legend>, or only wraps one: all its `text` is the label's. */
function isLabelElement(node: Element, text: string): boolean {
  if (node.matches("label, legend")) return true;
  const inner = node.querySelector("label, legend");
  return inner !== null && cleanText(inner.textContent) === text;
}

export function nearbyText(el: HTMLElement): string {
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "INPUT", "SELECT", "TEXTAREA", "BUTTON", "OPTION"]);
  let node: HTMLElement | null = el;
  for (let depth = 0; depth < 3 && node; depth++) {
    let sib: ChildNode | null = node.previousSibling;
    let hops = 0;
    while (sib && hops < 6) {
      let text = "";
      if (sib.nodeType === Node.TEXT_NODE) {
        text = cleanText(sib.textContent);
      } else if (sib.nodeType === Node.ELEMENT_NODE && !SKIP_TAGS.has((sib as Element).tagName)) {
        text = cleanText(sib.textContent);
      }
      // Long blobs are paragraphs/descriptions, not labels; placeholder filler
      // ("Select…") is not a label either, skip both and keep scanning. A
      // <label> is a label at any length: Epic Games' 180-character
      // sponsorship question was skipped for the question before it (live
      // 2026-10-05).
      const max = sib.nodeType === Node.ELEMENT_NODE && isLabelElement(sib as Element, text) ? 400 : 160;
      if (text && text.length <= max && !isPlaceholderFiller(text)) return text;
      sib = sib.previousSibling;
      hops++;
    }
    node = node.parentElement;
  }
  return "";
}

/**
 * A "form field block": the wrapper an ATS emits around ONE field, its
 * question and its control together.
 *
 * Deliberately restricted to markers that are proven to wrap exactly one field.
 * `data-automation-id="formField-*"` and `data-fkit-id` are Workday's, and
 * `test/fixtures/workdayReal.ts` (captured verbatim from a live tenant) shows
 * one per field: `formField-countryPhoneCode`, `formField-phoneType`,
 * `formField-source`. That same capture is why `[role="group"]` is NOT here,
 * there it wraps a whole SECTION, and a section's heading is not this field's
 * question.
 */
const FIELD_BLOCK_SELECTOR =
  '[data-automation-id^="formField" i], [data-fkit-id], fieldset';

/**
 * A control that could own a question of its own, as opposed to the widget
 * plumbing an ATS renders beside its real control. Workday's prompt ships a bare
 * `<input type="text">` mirror carrying no name, id, or ARIA: it is machinery,
 * not a field, and must not stop the climb below.
 */
const CONTROL_SELECTOR = "input, select, textarea, button, [role='combobox'], [contenteditable='true']";

function isRealControl(node: Element): boolean {
  return Boolean(
    node.getAttribute("name") ||
      node.id ||
      node.getAttribute("aria-label") ||
      node.getAttribute("aria-labelledby") ||
      node.getAttribute("data-automation-id")
  );
}

/**
 * The block around `el` when the page marks up no wrapper we recognise: the
 * nearest ancestor that actually contributes question text, climbing only while
 * no OTHER real control appears.
 *
 * The stop condition is what keeps this honest. Climbing blind would eventually
 * reach a section (or the page) and hand this field its neighbour's question.
 * Stopping at the first ancestor that adds text of its own means the widget's
 * empty nesting divs are climbed through and nothing beyond the field is.
 */
function implicitBlockOf(el: HTMLElement): HTMLElement | null {
  const own = cleanText(el.textContent);
  let node = el.parentElement;
  for (let hops = 0; node && hops < 5; hops++, node = node.parentElement) {
    const others = [...node.querySelectorAll(CONTROL_SELECTOR)].filter(
      (c) => c !== el && !el.contains(c) && isRealControl(c)
    );
    if (others.length > 0) return null; // a neighbour's block, not ours
    let text = cleanText(node.textContent);
    if (own && text.endsWith(own)) text = cleanText(text.slice(0, -own.length));
    if (text) return node;
  }
  return null;
}

/** The field block `el` sits in, nearest wins, so a block nested in a section
 *  resolves to the block. Falls back to a structural block when the page marks
 *  up none; null when even that cannot be decided. */
function fieldBlockOf(el: HTMLElement): HTMLElement | null {
  const marked = el.parentElement?.closest<HTMLElement>(FIELD_BLOCK_SELECTOR) ?? null;
  // A marked block holding OTHER controls is a section of the form, not this
  // field's block: Pinpoint's "1. Personal Details … Apply with LinkedIn"
  // <fieldset> labelled four address boxes "LinkedIn" (live 2026-10-03). The
  // field's own unmarked wrapper ("Address Line 1" + its input) is the block.
  if (marked && sharesBlockWithOthers(marked, el)) return implicitBlockOf(el) ?? marked;
  return marked ?? implicitBlockOf(el);
}

/** Inputs a user fills in; a hidden mirror (Workday's) or a button is not one. */
const USER_INPUT_SELECTOR =
  'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="image"]), select, textarea';

/** `block` holds another visible input besides `el` (and outside `el`'s own subtree). */
function sharesBlockWithOthers(block: HTMLElement, el: HTMLElement): boolean {
  return [...block.querySelectorAll<HTMLElement>(USER_INPUT_SELECTOR)].some(
    (c) => c !== el && !el.contains(c) && !c.contains(el) && !hiddenFromReaders(c) && isVisible(c)
  );
}

/**
 * The question text inside a field block, ignoring the control itself.
 *
 * This is the signal that reaches what `nearbyText` structurally cannot. A
 * Workday prompt nests its button three `<div>`s below the block (verbatim in
 * the capture), and `nearbyText` climbs three ancestors looking at PREVIOUS
 * SIBLINGS: so it runs out of climb exactly one level short of the block whose
 * first child is the `<label>`. That is how a self-identification question fell
 * through to the widget's own aria-label and then to its raw id.
 *
 * `aria-hidden` subtrees are skipped because they are not part of any accessible
 * name, which also drops Workday's `<abbr aria-hidden="true">*</abbr>` required
 * marker, so the question reads as "Gender Identity", not "Gender Identity*".
 */
function blockQuestionText(block: HTMLElement, el: HTMLElement): string {
  const visibleText = (node: Element): string => {
    if (hiddenFromReaders(node)) return "";
    let out = "";
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) out += child.textContent ?? "";
      else if (child.nodeType === Node.ELEMENT_NODE) out += visibleText(child as Element);
    }
    return out;
  };

  // A <label>/<legend> that isn't wrapping the control is the question outright.
  for (const node of block.querySelectorAll("label, legend")) {
    if (node.contains(el)) continue;
    const text = cleanText(visibleText(node));
    if (text && !isPlaceholderFiller(text)) return text;
  }

  // No labelling element: the block's own text, minus the widget's. Workday's
  // hidden mirror <input> and menu glyph contribute nothing, so what is left is
  // the question the user reads.
  const own = cleanText(el.textContent);
  let text = cleanText(visibleText(block));
  if (own && text.endsWith(own)) text = cleanText(text.slice(0, -own.length));
  if (text && text.length <= 300 && !isPlaceholderFiller(text)) return text;

  // The question sits OUTSIDE the block (a heading above it). nearbyText from
  // the block (not from the control) is the one that can see it.
  return nearbyText(block);
}

/**
 * The question carried by an `aria-label`, with the widget's own boilerplate
 * removed.
 *
 * Workday writes `aria-label="<question> <displayed value> Required"`. The
 * verbatim capture has both halves of the proof: with a question
 * ("How Did You Hear About Us? Select One Required") and, from production,
 * without one ("Select One Required", "Yes Required"). Stripping the trailing
 * value and "Required" therefore both RECOVERS a real question when one is
 * there and yields "" when the attribute is pure boilerplate, which is the
 * honest answer, and stops "Yes Required" from being asked as a question.
 */
function ariaLabelQuestion(el: HTMLElement, ariaLabel: string): string {
  let text = ariaLabel;
  if (!text) return "";
  // Trailing required marker, however the tenant words it.
  text = cleanText(text.replace(/[\s*]*\(?\brequired\b\)?[\s*.]*$/i, ""));
  // Trailing copy of what the widget itself displays ("Select One", "Yes").
  //
  // Only for a CHOICE widget, and that restriction is load-bearing: what such a
  // widget displays is its ANSWER, so an aria-label that adds nothing to it
  // carries no question. On an ordinary control the visible text may well BE the
  // name of the thing, and stripping it would throw away a good label.
  const own = cleanText(el.textContent);
  if (isChoiceWidget(el) && own && text.toLowerCase().endsWith(own.toLowerCase())) {
    text = cleanText(text.slice(0, -own.length));
  }
  return isPlaceholderFiller(text) ? "" : text;
}

/** A control whose displayed text is a selected value rather than a name. */
function isChoiceWidget(el: HTMLElement): boolean {
  if (el instanceof HTMLSelectElement) return true;
  const role = (el.getAttribute("role") || "").toLowerCase();
  const haspopup = (el.getAttribute("aria-haspopup") || "").toLowerCase();
  return haspopup === "listbox" || role === "combobox" || role === "listbox";
}

/**
 * Custom dropdowns (react-select, Headless UI, Workday button-listboxes…) nest
 * the operable control, a tiny `role="combobox"` input or an
 * `aria-haspopup="listbox"` button, several layers inside a widget wrapper, and
 * the field's real `<label>` sits as a sibling of that WRAPPER, not the inner
 * control. Label discovery run from the inner control never climbs far enough
 * (and trips over the "Select…" placeholder on the way). So for combobox-like
 * controls we resolve labels from the outermost widget wrapper instead.
 */
function dropdownWidgetHost(el: HTMLElement): HTMLElement {
  const role = (el.getAttribute("role") || "").toLowerCase();
  const haspopup = (el.getAttribute("aria-haspopup") || "").toLowerCase();
  if (role !== "combobox" && haspopup !== "listbox") {
    return el.getAttribute("aria-autocomplete") === "list" && isLegacyReactSelect(el) ? legacyReactSelectHost(el) : el;
  }

  const WIDGET_CLASS = /select|combobox|dropdown|autocomplete/i;
  let host = el;
  let node = el.parentElement;
  for (let hops = 0; node && hops < 6; hops++) {
    const cls = node.getAttribute("class") || "";
    const nodeRole = (node.getAttribute("role") || "").toLowerCase();
    const isWidget =
      WIDGET_CLASS.test(cls) ||
      nodeRole === "combobox" ||
      nodeRole === "listbox" ||
      (node.getAttribute("aria-haspopup") || "").toLowerCase() === "listbox";
    if (!isWidget) break; // reached the field container, its sibling is the label
    host = node;
    node = node.parentElement;
  }
  return host;
}

/**
 * react-select before v5 (see isLegacyReactSelect): its "css-…-container",
 * then every wrapper around it up to the field's block, the first ancestor
 * holding a <label>/<legend> or another control. Epic Games' form puts the
 * question's <label> (whose `for` names no element) four wrappers above the
 * widget, and those wrappers gain text of their own along the way: "This
 * section is required" once the field is left empty, a description link
 * (live 2026-10-05).
 */
function legacyReactSelectHost(el: HTMLElement): HTMLElement {
  let host: HTMLElement = el;
  for (let node = el.parentElement, i = 0; node && i < 6; i++, node = node.parentElement) {
    if (/(^|\s)css-[a-z0-9]+-container(\s|$)/.test(node.getAttribute("class") ?? "")) {
      host = node;
      break;
    }
  }
  for (let hops = 0; hops < 8 && host.parentElement; hops++) {
    const parent: HTMLElement = host.parentElement;
    if (parent.querySelector("label, legend")) break;
    const others = [...parent.querySelectorAll(CONTROL_SELECTOR)].filter((c) => !host.contains(c) && isRealControl(c));
    if (others.length > 0) break;
    host = parent;
  }
  return host;
}

/** The one <label>/<legend> in the block around a legacy react-select's host
 *  (see legacyReactSelectHost), when the block holds no other control. */
function legacyReactSelectLabel(host: HTMLElement): string {
  const block = host.parentElement;
  if (!block) return "";
  const labels = [...block.querySelectorAll("label, legend")].filter((l) => !l.contains(host));
  if (labels.length !== 1) return "";
  const others = [...block.querySelectorAll(CONTROL_SELECTOR)].filter((c) => !host.contains(c) && isRealControl(c));
  if (others.length > 0) return "";
  const text = cleanText(labels[0].textContent);
  return text && !isPlaceholderFiller(text) ? text : "";
}

/** All the text signals the field matcher scores against. */
export interface FieldSignals {
  label: string;
  ariaLabel: string;
  placeholder: string;
  nearby: string;
  nameAttr: string;
  idAttr: string;
  autocomplete: string;
  /** Native input type ("email", "tel", "url"…), a strong category hint. */
  typeHint: string;
  /** Developer-assigned test ids (Workday's data-automation-id, data-testid…),
   *  stable semantic anchors when labels are generic or missing. */
  testId: string;
}

/**
 * First present developer-assigned test id. Workday's `data-automation-id` is the
 * most valuable; the `data-testid` / `data-test` / `data-qa` family covers most
 * React/Vue/Angular apps. These are author-declared semantics, so they make a
 * strong matching signal where visible labels are generic or missing.
 */
function testIdOf(el: HTMLElement): string {
  for (const attr of ["data-automation-id", "data-testid", "data-test", "data-qa"]) {
    const v = el.getAttribute(attr);
    if (v) return v;
  }
  return "";
}

/**
 * A drag-and-drop / "Select file" upload widget hides its real <input type=file>
 * behind a styled zone, and the input itself is usually unlabeled, the
 * describing text ("Upload your resume", "Drop file here") lives on the
 * surrounding zone. True when `el` is the hidden file input of such a widget,
 * so the scanner can still surface it (Workday, Greenhouse, Ashby dropzones).
 */
const UPLOAD_HINT = /file.?upload|fileupload|attach|resume|\bcv\b|drop.?zone|upload/i;

export function isUploadAffordance(el: HTMLElement): boolean {
  if (!(el instanceof HTMLInputElement) || el.type !== "file" || el.disabled) return false;
  const testId = el.getAttribute("data-automation-id") || el.getAttribute("data-testid") || "";
  const cls = el.className || "";
  if (UPLOAD_HINT.test(testId) || UPLOAD_HINT.test(cls)) return true;
  // Climb a few wrappers looking for an upload zone marker (testId/class).
  let node: HTMLElement | null = el.parentElement;
  for (let i = 0; i < 4 && node; i++) {
    const id = node.getAttribute("data-automation-id") || node.getAttribute("data-testid") || "";
    if (UPLOAD_HINT.test(id) || UPLOAD_HINT.test(node.className || "")) return true;
    node = node.parentElement;
  }
  return /upload|drop file|select file|attach|resume|\bcv\b|drag/i.test(nearbyText(el));
}

/**
 * Describing text of the upload widget wrapping a hidden file input, e.g.
 * Workday's "Upload your resume…" heading, which sits a wrapper or two ABOVE the
 * drop zone, not on the input. Climbs ancestors and returns the first container
 * that names a document (resume/CV/cover letter); else the nearest small wrapper.
 */
export function uploadZoneText(el: HTMLElement): string {
  if (!(el instanceof HTMLInputElement) || el.type !== "file") return "";
  const DOC = /resume|résumé|curriculum vitae|\bcv\b|cover letter/i;
  let node: HTMLElement | null = el.parentElement;
  let widest = "";
  for (let i = 0; i < 5 && node; i++) {
    const t = cleanText(node.textContent).slice(0, 300);
    if (t && DOC.test(t)) return t; // explicit document text → best signal
    if (t && t.length <= 300) widest = t;
    node = node.parentElement;
  }
  return widest;
}

/**
 * A web component's own label attribute. SmartRecruiters' `<spl-input
 * label="First name">` renders its <label> inside its shadow root, and a scan
 * taken before that render (a document observer never sees a shadow root
 * change) read every field by its id: "first-name-input", and
 * "spl-form-element_10" for City, which went unrecognized and blank (live
 * regression 2026-10-08). The nearest host up to two shadow roots out.
 */
function shadowHostLabel(el: HTMLElement): string {
  let node: Node = el;
  for (let hops = 0; hops < 2; hops++) {
    const root = node.getRootNode();
    if (!(root instanceof ShadowRoot)) return "";
    const own = cleanText(root.host.getAttribute("label"));
    if (own) return own;
    node = root.host;
  }
  return "";
}

export function collectSignals(el: HTMLElement): FieldSignals {
  const labelledBy = ariaLabelledByText(el);
  const isFile = el instanceof HTMLInputElement && el.type === "file";
  // Custom dropdowns bury the operable control deep inside a widget wrapper; the
  // real label lives beside that wrapper, so resolve labels/nearby from it.
  const host = dropdownWidgetHost(el);
  const isDropdown = host !== el;
  const legacy = isDropdown && el.getAttribute("aria-autocomplete") === "list" && !el.hasAttribute("role");
  const hostLabel = isDropdown ? associatedLabelText(host) || (legacy ? legacyReactSelectLabel(host) : "") : "";
  const hostLabelledBy = isDropdown ? ariaLabelledByText(host) : "";
  // A hidden upload input's identity lives on its zone, so fold the zone's
  // describing text into `nearby` for classification (e.g. "…your resume…").
  const nearby = isFile
    ? [nearbyText(el), uploadZoneText(el)].filter(Boolean).join(" ").slice(0, 220)
    : nearbyText(host);
  const assocLabel = associatedLabelText(el);
  const ariaLabel = ariaLabelQuestion(el, cleanText(el.getAttribute("aria-label")));
  // For a custom dropdown that carries NO programmatic label of any kind, the
  // text sitting right before the widget IS its question (that's how the form
  // reads visually), promote it to the reliable `label` signal. Without this it
  // would only land in weak `nearby` (0.6) and fall below the autofill bar (0.7),
  // so the field is classified yet never filled (country/gender/disability on
  // Greenhouse). Gated on the absence of every real label signal so a widget that
  // *does* declare an aria-label / association is never overridden by a stray
  // neighbouring label.
  const componentLabel = assocLabel || hostLabel || labelledBy || hostLabelledBy ? "" : shadowHostLabel(el);
  const hasRealLabel = Boolean(assocLabel || hostLabel || labelledBy || hostLabelledBy || ariaLabel || componentLabel);
  const promotedLabel = isDropdown && !hasRealLabel ? nearby : "";
  // Last resort before the weak signals: the question printed inside the field's
  // own block. Computed only when nothing programmatic named the field, so a
  // field that IS labelled keeps the label it already had, and `nearby` is left
  // exactly as it was, since it feeds the classifier at its own weight.
  const blockLabel =
    hasRealLabel || promotedLabel
      ? ""
      : (() => {
          const block = fieldBlockOf(el);
          return block ? blockQuestionText(block, el) : "";
        })();
  return {
    label:
      assocLabel || hostLabel || labelledBy || hostLabelledBy || componentLabel || promotedLabel || blockLabel,
    ariaLabel: ariaLabel || labelledBy || hostLabelledBy,
    placeholder: cleanText(el.getAttribute("placeholder")),
    nearby,
    nameAttr: el.getAttribute("name") ?? "",
    idAttr: el.id ?? "",
    autocomplete: (el.getAttribute("autocomplete") ?? "").trim().toLowerCase(),
    typeHint: el instanceof HTMLInputElement ? el.type : "",
    testId: testIdOf(el),
  };
}

/**
 * Pick the most human-readable label for display in the popup. Placeholder
 * filler ("Select…", "Choose an option") is never a usable question text, a
 * dropdown whose only signal is its own placeholder must fall through to the
 * name/id attributes rather than present "Select" as the question.
 *
 * The name/id fallback stops at machine ids. An attribute name like
 * `candidate_country` is a poor label but still says what the field is; Workday's
 * `56370316e58a1001d8aa4cd7b1d70000-b0531cc2ff371001d8a9b9c2eef00002` says
 * nothing, and reaching it means we genuinely could not name the field, which
 * the sentinel states plainly instead of dressing an id up as a question.
 */
export function bestDisplayLabel(signals: FieldSignals): string {
  const candidates = [
    signals.label,
    signals.ariaLabel,
    signals.placeholder,
    signals.nearby,
    signals.nameAttr,
    signals.idAttr,
  ];
  for (const c of candidates) {
    if (c && !isPlaceholderFiller(c) && !isMachineId(c)) return c;
  }
  return UNLABELED_FIELD;
}

export function isRequiredField(el: HTMLElement, signals: FieldSignals): boolean {
  if ((el as HTMLInputElement).required) return true;
  if (el.getAttribute("aria-required") === "true") return true;
  return /[*✱]\s*$/.test(signals.label) || /[*✱]\s*$/.test(signals.nearby);
}

// ---------------------------------------------------------------------------
// Value writing: must look like real user input to React/Vue/Angular
// ---------------------------------------------------------------------------

/**
 * Set .value so the framework backing the control registers a real user write.
 *
 * For the standard form elements this goes through the native PROTOTYPE setter:
 * React overrides `value` on the instance to track programmatic writes, so
 * assigning `el.value` directly is swallowed, calling the prototype's setter
 * with the instance as `this` is what makes React/Vue/Angular see user input.
 *
 * Custom elements (ADP's `sdf-select-simple`, Lightning, some Angular-Material
 * widgets) don't inherit from the standard prototypes; their `value` setter
 * lives on their own class prototype, or they only accept a `value` attribute /
 * a `setValue()` method. Walk the prototype chain for a real setter, then fall
 * back through instance assignment → attribute → setValue()/setAttributeValue().
 * Each strategy is best-effort: a readonly own-property or a missing method must
 * not abort the ones after it.
 */
export function setNativeValue(el: HTMLElement, value: string): void {
  const std =
    el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : el instanceof HTMLInputElement
          ? HTMLInputElement.prototype
          : null;
  if (std) {
    const setter = Object.getOwnPropertyDescriptor(std, "value")?.set;
    if (setter) {
      setter.call(el, value);
      return;
    }
  }
  // Non-standard / custom element: a `value` setter may sit anywhere on its own
  // prototype chain (react-aware widgets define one there).
  if (setValueViaPrototypeChain(el, value)) return;
  // Last-resort strategies for web components with no discoverable setter.
  const anyEl = el as unknown as {
    value?: unknown;
    setValue?: (v: string) => void;
    setAttributeValue?: (v: string) => void;
  };
  try {
    anyEl.value = value;
  } catch {
    /* value is a readonly own property, try the attribute path */
  }
  try {
    el.setAttribute("value", value);
  } catch {
    /* some elements reject setAttribute('value'), ignore */
  }
  try {
    anyEl.setValue?.(value);
  } catch {
    /* custom setter threw, ignore */
  }
  try {
    anyEl.setAttributeValue?.(value);
  } catch {
    /* custom setter threw, ignore */
  }
}

/** Walk the prototype chain looking for a `value` setter and call it with `el`
 *  as receiver. Returns true if one was found and invoked. */
function setValueViaPrototypeChain(el: HTMLElement, value: string): boolean {
  let proto: object | null = Object.getPrototypeOf(el);
  while (proto && proto !== Object.prototype) {
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc?.set) {
      desc.set.call(el, value);
      return true;
    }
    proto = Object.getPrototypeOf(proto);
  }
  return false;
}

/**
 * Fire the events frameworks listen for. React uses "input"; Angular and
 * many validation libraries also want "change" and "blur". `composed: true`
 * lets the event escape an open shadow root so handlers delegated at the
 * document (Workday, many web components) actually receive it.
 */
export function dispatchInputEvents(el: HTMLElement, value?: string): void {
  el.dispatchEvent(
    new InputEvent("input", {
      bubbles: true,
      composed: true,
      data: value ?? null,
      inputType: "insertText",
    })
  );
  el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
}

/**
 * Fire Enter keydown/keyup on a control. This is what commits an autocomplete /
 * typeahead selection, applies an input mask, and wakes "validate on Enter"
 * handlers, the value is already set through the native setter, so this is the
 * belt-and-suspenders that makes stubborn framework fields register it.
 *
 * The `KeyboardEvent` constructor drops `keyCode`/`which` (they always read 0),
 * yet a lot of legacy handlers still branch on `keyCode === 13`. Force both to
 * 13 with defineProperty so those paths run. Synthetic keyboard events never
 * trigger the browser's own implicit form submission, so this cannot submit.
 */
export function dispatchCommitKeys(el: HTMLElement): void {
  for (const type of ["keydown", "keyup"] as const) {
    const ev = new KeyboardEvent(type, {
      key: "Enter",
      code: "Enter",
      bubbles: true,
      cancelable: true,
      composed: true,
    });
    Object.defineProperty(ev, "keyCode", { get: () => 13 });
    Object.defineProperty(ev, "which", { get: () => 13 });
    el.dispatchEvent(ev);
  }
}

/** Briefly outline a filled control so the user can review what changed. */
export function flashHighlight(el: HTMLElement): void {
  const doc = el.ownerDocument;
  if (!doc.getElementById("ap-autofill-style")) {
    const style = doc.createElement("style");
    style.id = "ap-autofill-style";
    style.textContent = `
      [data-ap-flash] {
        outline: 2px solid #533afd !important;
        outline-offset: 1px;
        transition: outline-color 0.4s ease;
      }`;
    doc.documentElement.appendChild(style);
  }
  el.setAttribute("data-ap-flash", "");
  setTimeout(() => el.removeAttribute("data-ap-flash"), 2500);
}
