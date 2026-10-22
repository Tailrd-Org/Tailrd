/**
 * What KIND of answer a field takes, decided BEFORE any value is resolved,
 * and the gate that refuses a value of the wrong kind.
 *
 * A field's category says what it is ABOUT; its kind says what it ACCEPTS.
 * "Are you currently located in Quebec?" is about location and accepts
 * Yes/No; writing "Quebec" into it is the wrong-kind failure this module
 * exists to make impossible. The kind comes from the control (a native
 * <input type="email"> takes an email, a radio group takes one of its
 * options), from the question's grammar (a question that opens with an
 * auxiliary verb takes Yes/No), and from the options when they are known
 * (a Yes/No option set is boolean whatever the label says).
 */
import type { ControlType } from "../shared/types";

/** Where a multi-select answer splits into its items: a comma, a semicolon or
 *  a line break, but never a thousands comma ("$185,000" was "$185" and
 *  "000": Choice Solutions on Paylocity, bank, round 8). */
export const ANSWER_LIST_SEPARATOR = /[;\n]+|(?<!\d),|,(?!\d{3}(?!\d))/;

export type AnswerKind =
  | "boolean"
  | "choice"
  | "multiChoice"
  | "text"
  | "longText"
  | "number"
  | "date"
  | "email"
  | "phone"
  | "url"
  | "file"
  | "password";

export interface KindInput {
  controlType: ControlType;
  /** Native input type ("email", "number", "date"…) when the control is an <input>. */
  inputType?: string;
  label: string;
  helpText?: string;
  placeholder?: string;
  options?: string[];
  multi?: boolean;
}

/** Lowercase, punctuation → space. */
function norm(text: string): string {
  return (text || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[’']/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/**
 * Decline-to-answer options carry negation words ("Prefer NOT to say") that
 * must never be read as a "No".
 */
// "Do not wish / want" declines only to answer: "No, I do not wish to relocate
// or support onsite." (Space Dynamics Lab on JazzHR, bank, round 8) is a No.
const DECLINE_RE =
  /\b(prefer not|rather not|decline|choose not|not (to )?(say|answer|disclose|specify)|(do not|dont|not) (wish|want)( to (answer|say|disclose|share|provide|specify|identify|self identify|respond|state|declare|be identified|complete|participate|fill))?$|(do not|dont|not) (wish|want) to (answer|say|disclose|share|provide|specify|identify|self identify|respond|state|declare|be identified|complete|participate|fill)|do not know|dont know|undisclosed|no answer|not applicable|n a|unsure|not sure|maybe|unknown)\b|^(na|n a|other)$/;

/**
 * Yes/No polarity of an option or answer text, or null when it has none.
 *
 *   "Yes" / "YES" / "Yes, I am authorized…"          → true
 *   "No" / "No, I do not require sponsorship"          → false
 *   "I am authorized to work…"                         → true
 *   "I am NOT authorized…" / "I do not require…"       → false
 *   "Prefer not to say" / "Decline to self-identify"   → null
 *   "Maybe" / "Canada"                                 → null
 */
export function optionPolarity(text: string): boolean | null {
  const t = norm(text);
  if (!t) return null;
  if (DECLINE_RE.test(t)) return null;
  if (/^(yes|y|true|oui|si|correct|affirmative|agree|accept|accepted|consent)\b/.test(t)) return true;
  if (/^(disagree|do not agree|dont agree|reject)\b/.test(t)) return false;
  // "non" only on its own (French "no"): "Non-binary" is not a No.
  if (/^(no|n|not|never|false|none|incorrect)\b/.test(t) || t === "non") return false;
  if (/^(i|we)\b/.test(t)) {
    if (/\b(not|never|dont|doesnt|didnt|wont|cannot|cant|havent|hasnt|isnt|arent|wasnt|no longer|am not|do not|will not|would not|have not|unable)\b/.test(t)) {
      return false;
    }
    if (/^(i|we) (am|do|will|would|have|can|require|need|currently|hold|possess|agree|accept|confirm|certify|understand|acknowledge|consent|authori[sz]e|give|grant)\b/.test(t)) return true;
  }
  return null;
}

/** An option that only gives or refuses consent: "I consent", "I do not
 *  Consent", "Yes, I agree", "I acknowledge". A question offering nothing
 *  else is an acknowledgement, whatever its paragraph mentions. */
// "I understand and accept these terms" too (Space Dynamics Lab on JazzHR,
// bank, round 8: its disability sentence made it a self-ID question).
export function isConsentOption(text: string): boolean {
  return /^(yes,? |no,? )?(i )?((have read|read|understand|understood)( and| &|,)? )?(do not |dont |will not |refuse to )?(consent|agree|acknowledge|accept|certify|authori[sz]e)\b/.test(norm(text));
}

/** An option set that is a Yes/No choice (possibly with a decline / maybe option). */
export function isBooleanOptionSet(options: string[] | undefined): boolean {
  if (!options || options.length < 2) return false;
  const real = options.filter((o) => norm(o) && !/^(select|choose|please select|select an option|select one)\b/.test(norm(o)));
  if (real.length < 2 || real.length > 4) return false;
  const pol = real.map(optionPolarity);
  const yes = pol.filter((p) => p === true).length;
  const no = pol.filter((p) => p === false).length;
  // Exactly one Yes and one No; anything else must be a decline / maybe.
  return yes === 1 && no === 1;
}

/**
 * A label that asks a yes/no question: it opens with an auxiliary verb
 * addressed to the applicant ("Are you…", "Do you…", "Will you now or in the
 * future…", "Have you ever…"), possibly after a short preamble sentence.
 *
 * A question that ALSO asks for detail ("If so, please list", "If yes, who?")
 * is not boolean: a bare Yes/No would not answer it.
 */
// Whether "this" or "that" suits them is one too: "…, is this something that
// will work for you?" (Solace on BambooHR, bank, round 8), "Does that work
// for you?" (Clay on Ashby). "Was it a job board or an employee?" (Credence on
// Workable) asks which.
const AUX_START = /^(are|is|do|does|did|have|has|had|will|would|can|could|should|may|might|were|was|shall)\s+(you|your|u|the applicant|the candidate|they)\b|^(is|does|will|would|can|could)\s+(this|that|it)\s+(?:\w+\s+){0,4}?(work|works|suit|suits|fit|fits|acceptable|okay|ok|feasible|possible)\b/;
const DETAIL_FOLLOWUP = /\b(if (so|yes|applicable|not)|please (list|explain|describe|specify|provide|indicate|elaborate|share|tell)|which one|who|what (is|was|are)|where|when|how many|how long|please state)\b/;

export function isYesNoQuestion(label: string): boolean {
  const raw = (label || "").trim();
  if (!raw) return false;
  // Split into sentences; any sentence may carry the actual question
  // ("This role is in Toronto. Are you able to commute?"), and so may a
  // clause after a comma ("This position would be 2 days in-office in the
  // Ottawa area, is this something that will work for you?": Solace on
  // BambooHR, bank, round 8, took the current title in a text box).
  // (Only "this", "that" or "it" asked of there: "…, can you provide further
  // details…" is a request.)
  const sentences = raw.split(/(?<=[.?!:])\s+|,\s+(?=(?:is|does|will|would)\s+(?:this|that|it)\b)/i).map(norm).filter(Boolean);
  const asks = sentences.some((s) => AUX_START.test(s));
  if (!asks) return false;
  return !DETAIL_FOLLOWUP.test(norm(raw));
}

const CHOICE_CONTROLS: ReadonlySet<ControlType> = new Set<ControlType>([
  "select",
  "radioGroup",
  "ariaRadioGroup",
  "combobox",
  "customDropdown",
]);

const DATE_LABEL = /\b(date|dob|birthday|when can you start|start(ing)? date|available from|availability date)\b/;
const NUMBER_LABEL =
  /^(how many|number of|total number of|years of|age)\b|\bhow many (years|months)\b|\b(total )?number of years\b|\byears of [a-z ]{0,40}experience\b/;

/**
 * The kind of answer this field accepts. Choice controls are boolean when
 * their options are a Yes/No pair (or, with options unknown, when the label
 * asks a yes/no question); a free-text input is boolean when the label asks a
 * yes/no question with no detail follow-up.
 */
export function answerKindOf(input: KindInput): AnswerKind {
  const { controlType, inputType = "", label, options, multi } = input;
  if (controlType === "file") return "file";
  if (controlType === "password") return "password";
  if (controlType === "checkbox") return "boolean";
  if (controlType === "checkboxGroup") return "multiChoice";
  if (CHOICE_CONTROLS.has(controlType)) {
    if (multi) return "multiChoice";
    if (options && options.length > 0) return isBooleanOptionSet(options) ? "boolean" : "choice";
    return isYesNoQuestion(label) ? "boolean" : "choice";
  }
  const type = inputType.toLowerCase();
  if (type === "email") return "email";
  if (type === "tel") return "phone";
  if (type === "url") return "url";
  if (type === "number") return "number";
  if (type === "date" || type === "month" || type === "datetime-local") return "date";
  const isLong = controlType === "textarea" || controlType === "contenteditable";
  if (isYesNoQuestion(label)) return "boolean";
  const l = norm(label);
  if (!isLong && DATE_LABEL.test(l) && !/\b(update|format)\b/.test(l)) return "date";
  if (!isLong && NUMBER_LABEL.test(l)) return "number";
  return isLong ? "longText" : "text";
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const URL_RE = /^(https?:\/\/)?([a-z0-9-]+\.)+[a-z]{2,}(\/\S*)?$/i;

/**
 * Whether `value` is an acceptable answer of `kind`. Choice kinds are checked
 * against their options elsewhere (strict option matching); here they only
 * reject what can never be a choice (an empty string).
 */
export function valueFitsKind(value: string, kind: AnswerKind): boolean {
  const v = (value || "").trim();
  if (!v) return false;
  switch (kind) {
    case "boolean":
      return optionPolarity(v) !== null || /^(checked|unchecked|agree|1|0)$/i.test(v);
    case "email":
      return EMAIL_RE.test(v);
    case "phone": {
      const digits = v.replace(/\D/g, "");
      return digits.length >= 7 && digits.length <= 15 && /^[+\d\s().\-–/x]+$/i.test(v.replace(/\bext\.?\b/i, ""));
    }
    case "url":
      return URL_RE.test(v);
    case "number":
      return /^-?\d+(\.\d+)?$/.test(v.replace(/[,\s]/g, ""));
    case "date":
      return /\d/.test(v) && v.length <= 40;
    case "file":
    case "password":
      return false;
    default:
      return true;
  }
}
