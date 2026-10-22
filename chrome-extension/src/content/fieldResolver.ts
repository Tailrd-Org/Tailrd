/**
 * One field's deterministic answer, decided in a fixed order:
 *
 *   1. KIND first (answerKind.ts): what this field accepts.
 *   2. QUESTION shapes (questionResolver.ts): screening questions answered
 *      from derived facts. An explicit abstain ENDS resolution: the field stays
 *      blank and is never handed to the backend, whose rule pass would guess
 *      (it answers "authorized to work?" Yes for everyone).
 *   3. CATEGORY value (adapter hook, then the generic profile resolver).
 *   4. GATE: a value of the wrong kind is dropped ("Quebec" never reaches a
 *      Yes/No field); a choice control only ever gets one of its REAL options,
 *      matched strictly, or nothing.
 *
 * Every value this returns is safe to write without review; anything less
 * certain comes back null.
 */
import type { ControlType, FieldCategory, ResolveControl, UserApplicationProfile } from "../shared/types";
import { resolveAnswerWithAdapter } from "./adapters/apply";
import type { SiteAdapter } from "./adapters/types";
import { answerKindOf, optionPolarity, valueFitsKind, type AnswerKind } from "./answerKind";
import { dateFormatFor, fitDate, type DateFormat } from "./dateControl";
import { cleanText, type FieldSignals } from "./domUtils";
import { isHigh, profileFacts } from "./profileFacts";
import { resolveQuestion, type QuestionContext, type QuestionInput, type QuestionResult } from "./questionResolver";
import { countryFromName } from "./geo";
import { snapSchool } from "./schoolMatch";
import { placeOf } from "./placeMatch";
import { closestDemographicOption, declineOption, groupedEthnicityOption, hispanicRaceOption, severalHold, veteranOption } from "./demographicMatch";
import { matchOption } from "./writeEngine";
import { sentencesOf } from "./statementText";

export interface FieldResolveInput {
  adapter: SiteAdapter | null;
  category: FieldCategory;
  sensitive: boolean;
  profile: UserApplicationProfile | null;
  control: ResolveControl & { multi?: boolean };
  fillEEO: boolean;
  el: HTMLElement;
  /** The question text (bestDisplayLabel). */
  label: string;
  signals: FieldSignals;
}

export interface FieldResolution {
  value: string | null;
  kind: AnswerKind;
  /** "question" (computed from facts) | "category" (profile/adapter) | "none" */
  source: "question" | "category" | "none";
  rule?: string;
  /** The device recognized the question and decided it must stay blank. */
  deviceAbstained: boolean;
  /** The format of a date control (dateControl.ts), when the field is one. */
  dateFormat?: DateFormat;
}

let resolveContext: QuestionContext = { jobCountry: null, company: "" };

/** The page's job context (country, company), set by the content script. */
export function setResolveContext(ctx: Partial<QuestionContext>): void {
  resolveContext = { ...resolveContext, ...ctx };
}

export function getResolveContext(): QuestionContext {
  return resolveContext;
}

/** Categories whose answers are the user's own demographic statements; they
 *  keep their dedicated matcher (demographicMatch) and never take a computed
 *  answer. */
const PASS_THROUGH: ReadonlySet<FieldCategory> = new Set<FieldCategory>(["accountPassword", "resumeUpload"]);

/** Demographic questions answered "decline" when the profile holds no answer:
 *  it discloses nothing, and a required one no longer blocks the submit. */
const EEO_DECLINABLE: ReadonlySet<FieldCategory> = new Set<FieldCategory>([
  "eeoGender", "eeoRace", "eeoHispanic", "eeoVeteran", "eeoDisability",
  "eeoGenderIdentity", "eeoSexualOrientation", "eeoPronouns", "eeoOther",
]);
const CHOICE_CONTROLS: ReadonlySet<ControlType> = new Set<ControlType>([
  "select", "radioGroup", "ariaRadioGroup", "combobox", "customDropdown", "checkboxGroup",
]);
/** What a decline reads as before a lazy dropdown's options are known; the
 *  on-device demographic matcher maps it to the form's own wording. */
export const EEO_DECLINE = "Decline to self-identify";

/** "Do you identify as transgender?": its own question, never an LGBTQ+ one. */
const TRANS_Q = /\b(identify as|are you|consider yourself( to be)?)( a)? trans(gender)?\b|\btrans(gender)? (identity|status|experience)\b/i;
// "2SLGBTQI+" (Canada) and "2ELGBTQI+" (French) carry no word boundary before
// "LGBT" (Coveo, live 2026-10-05).
const LGBTQ_Q = /\b(?:2[se])?lgbt\w*|\b(queer|gay|lesbian|bisexual|sexual orientation)\b/i;

/**
 * Gender identity asked three ways (live 2026-10-03). "Do you identify as
 * transgender?" (Ashby) is answered from a cisgender / transgender identity.
 * Plain gender options ("Female", "Male", "Gender non-binary" on
 * PointClickCare; "Man", "Woman" on Ashby) take the GENDER answer, which an
 * identity like "Cisgender" is not one of. Qualified options ("Cisgender
 * woman") need both, so a cisgender woman never lands on "Cisgender man".
 * undefined = no refinement.
 */
function refineGenderIdentity(
  category: FieldCategory,
  label: string,
  options: string[] | undefined,
  profile: UserApplicationProfile
): string | null | undefined {
  const identity = (profile.eeo?.genderIdentity ?? "").trim();
  const gender = (profile.eeo?.gender ?? "").trim();
  // "Do you identify as LGBTQ+?": Yes for a stated orientation or identity
  // that is one; No only when BOTH are stated and neither is. A cisgender
  // identity says nothing about orientation, so it never answers alone.
  if (category === "eeoOther" && /\b(?:2[se])?lgbt/i.test(label)) {
    const orientation = (profile.eeo?.sexualOrientation ?? "").trim();
    if (/\b(gay|lesbian|bisexual|queer|pansexual)\b/i.test(orientation) || /\btrans(gender)?\b/i.test(identity)) return "Yes";
    if (/^(heterosexual|straight)$/i.test(orientation) && /\bcis(gender)?\b/i.test(identity)) return "No";
    return undefined;
  }
  if ((category === "eeoOther" || category === "eeoGenderIdentity") && TRANS_Q.test(label) && !LGBTQ_Q.test(label)) {
    if (/\bcis(gender)?\b/i.test(identity)) return "No";
    if (/\btrans(gender)?\b/i.test(identity)) return "Yes";
    return undefined;
  }
  if (!options?.length) return undefined;
  const qualified = options.filter((o) => /\b(cis|trans)(gender)?\b/i.test(o));
  // A gender question whose options say cis or trans with the gender
  // ("Cisgender Woman" | "Transgender Man", AltaML on Lever, question bank
  // 2026-10-08) is the identity question too.
  // Only when the stated gender is not offered plain: beside "Male", a trans
  // man's gender is "Male" (Tanium, Hootsuite), as he gave it.
  const plainOffered = Boolean(gender) && options.some((o) => !qualified.includes(o) && closestDemographicOption("eeoGender", gender, [o]) !== null);
  if (category !== "eeoGenderIdentity" && !(category === "eeoGender" && !plainOffered && qualified.some((o) => /\b(man|woman|male|female)\b/i.test(o)))) return undefined;
  if (qualified.length === 0) return /\b(male|female|man|woman)\b/i.test(options.join(" ")) && gender ? gender : undefined;
  const kind = /\bcis(gender)?\b/i.test(identity) ? /\bcis(gender)?\b/i : /\btrans(gender)?\b/i.test(identity) ? /\btrans(gender)?\b/i : null;
  // Options split by cis/trans and the profile states neither: "Cisgender
  // man" from "Male" claims an identity the user never gave (a real profile
  // on Robinhood, 2026-10-03). Only an unqualified option ("Man",
  // "Non-binary") may answer; else decline, as for any unanswered EEO question.
  if (!kind) {
    const plain = options.filter((o) => !qualified.includes(o));
    return gender ? closestDemographicOption(category, gender, plain) : null;
  }
  // Only transgender options are qualified ("Female" | "Male" | "Transgender-
  // Female", Braze; question bank 2026-10-05): a cisgender identity is the
  // plain gender.
  if (kind.source.includes("cis") && !qualified.some((o) => kind.test(o))) {
    const plain = options.filter((o) => !qualified.includes(o));
    return gender ? closestDemographicOption(category, gender, plain) : null;
  }
  const sex = /^(female|woman)$/i.test(gender) ? /\b(woman|female)\b/i : /^(male|man)$/i.test(gender) ? /\b(man|male)\b/i : null;
  if (!sex) return undefined;
  const both = qualified.filter((o) => kind.test(o) && sex.test(o));
  return both.length === 1 ? both[0] : undefined;
}

const APPLICANT_ADDRESS: ReadonlySet<FieldCategory> = new Set<FieldCategory>([
  "location", "addressStreet", "addressCity", "addressState", "postalCode", "country",
]);
/** A field named for an education or employment history row. */
const HISTORY_ROW = /education|school|employ|experience|work ?history|job ?history|workhistory/i;

const CURRENT_JOB_LABEL = /\b(current|currently|present|presently)\b/i;
const RECENT_JOB_LABEL = /\b(most recent|recent|previous|last|latest|former)\b/i;

/**
 * The company / title for a field outside a repeating row, read against the
 * DATED experience rows. The backend sends the resume's first job as the
 * current company whatever its end date: a real profile's internship that
 * ended in May 2026 was typed into "Current company" in October (Lever,
 * Commvault). So a label that says current gets a job still running or
 * nothing (null), and a bare "Company" / "Title" (Workable's experience
 * entry) gets the most recent job, its title from the same row as the
 * company. undefined = keep the value as resolved.
 */
function employmentForLabel(
  category: FieldCategory,
  label: string,
  profile: UserApplicationProfile,
  value: string | null
): string | null | undefined {
  const emp = profileFacts(profile).employment;
  const rows = (profile.experience ?? []).filter((r) => r && (r.company?.trim() || r.title?.trim()));
  const same = (a: string | undefined, b: string): boolean => (a ?? "").trim().toLowerCase() === b.trim().toLowerCase();
  if (CURRENT_JOB_LABEL.test(label) && !RECENT_JOB_LABEL.test(label)) {
    const ended = emp.currentlyEmployed?.value === false;
    const fromRow = value !== null && rows.some((r) => same(category === "currentCompany" ? r.company : r.title, value));
    return ended && fromRow ? null : undefined;
  }
  if (category === "currentTitle" && !value) {
    const company = isHigh(emp.currentCompany) ? emp.currentCompany.value : emp.mostRecentCompany?.value ?? "";
    const row = company ? rows.find((r) => same(r.company, company)) : undefined;
    return row?.title?.trim() || undefined;
  }
  return undefined;
}

function eeoDecline(category: FieldCategory, controlType: ControlType, options: string[] | undefined): string | null {
  if (!EEO_DECLINABLE.has(category) || !CHOICE_CONTROLS.has(controlType)) return null;
  const real = (options ?? []).filter((o) => o.trim());
  if (real.length > 0) return declineOption(real);
  return controlType === "combobox" || controlType === "customDropdown" ? EEO_DECLINE : null;
}

/** Categories that live in repeating employment / education rows. */
const ROW_CATEGORIES: ReadonlySet<FieldCategory> = new Set<FieldCategory>([
  "currentCompany", "currentTitle", "experienceStartDate", "experienceEndDate", "experienceDescription",
  "experienceCurrent", "school", "degree", "fieldOfStudy", "graduationYear",
]);

/** A second street line: "Address Line 2", "Address 2", "Apt / Suite". */
function isAddressLine2(label: string, signals: FieldSignals): boolean {
  if (/address-line2/i.test(signals.autocomplete ?? "")) return true;
  const text = `${label} ${signals.nameAttr} ${signals.idAttr}`;
  return /\b(address ?(line ?)?2|line ?2|addr(ess)?_?2|apt|apartment|suite|unit number)\b/i.test(text) || /\baddress2\b/i.test(text);
}

/** Categories whose answer is a single short fact. */
const SINGLE_LINE_FACTS: ReadonlySet<FieldCategory> = new Set<FieldCategory>([
  "firstName", "lastName", "fullName", "email", "phone", "location", "addressStreet", "addressCity",
  "addressState", "postalCode", "country", "linkedin", "github", "portfolio", "currentCompany", "currentTitle",
  "school", "degree", "fieldOfStudy", "graduationYear", "salary", "yearsOfExperience", "startDate", "noticePeriod",
  // A project to describe that shows "your data analysis experience/skills"
  // got the skills list twice (PDT on Greenhouse, live 2026-10-17).
  "skills",
]);

/** A label that is a prompt for prose rather than the name of a fact. */
function isProsePrompt(label: string): boolean {
  // A required mark is no word: "Where are you currently located? *" (Rhombus
  // Power, Greenhouse bank 3, 2026-10-08) counted six and lost the city.
  const words = (label || "").replace(/[*\u2731]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (words.length <= 5) return false;
  // A fact asked WITH an essay is still an essay: "What is your major? Please
  // describe why you feel it is applicable…" got the major alone
  // (NinjaHoldings on Breezy, live 2026-10-03).
  if (/\b(describe|description|explain|elaborate|tell us|why (do|did|would|are|is)|in your own words)\b/i.test(label)) return true;
  // A link asked for at length is still a link: "Please paste your LinkedIn
  // profile link here. Please confirm the link is accurate…" (Ammortal, Open
  // Data Jobs on Workable, 2026-10-05) was left blank.
  if (/\b(paste|enter|provide|share|add|include|insert|type)( in)? (the |a |your )?(full )?(web address|url|link|linked ?in|git ?hub|portfolio)\b|\b(url|link) (of|to|for) your\b/i.test(label)) return false;
  return !/\b(name of|what is your|what's your|your current|please (enter|provide|list|state) (your|the name))\b/i.test(label);
}

/** The sentence a label asks: its last question, else its last sentence. */
function askedSentenceOf(label: string): string {
  const parts = sentencesOf(label);
  return [...parts].reverse().find((s) => /\?\W*$/.test(s)) ?? parts[parts.length - 1] ?? "";
}

/** Facts a how/why or yes/no question may mention without asking for them. */
const MENTIONED_FACTS: ReadonlySet<FieldCategory> = new Set<FieldCategory>([
  "currentTitle", "currentCompany", "location", "addressCity", "addressState", "country", "school", "degree",
  "fieldOfStudy", "graduationYear", "salary",
]);

/** A how/why or yes/no question: its answer is not the fact its words name. */
function asksAnotherThing(label: string, category: FieldCategory): boolean {
  if (!MENTIONED_FACTS.has(category)) return false;
  const asked = askedSentenceOf(label);
  // "How much do you expect to earn?" asks the salary itself.
  if (/^(how|why)\b/i.test(asked)) return !(category === "salary" && /^how much\b/i.test(asked));
  return /^(are|is|do|does|did|have|has|can|could|will|would)\b/i.test(asked);
}

/** Experience asked in general: a work history, not one kind of it. */
const GENERAL_EXPERIENCE =
  /\b(work|employment|professional|relevant|previous|prior|career|job)\s+(experience|history)\b|^\s*experience\s*\*?\s*$|\b(describe|tell us about|summari[sz]e|list) your (relevant |work |professional )?experience\s*[.?:*]*\s*$/i;

/** Controls whose options are fully known at scan time. */
const CONSTRAINED: ReadonlySet<ControlType> = new Set<ControlType>(["select", "radioGroup", "ariaRadioGroup", "checkboxGroup"]);

const pick = (options: string[], value: string): string | null => matchOption(options, (o) => o, (o) => o, value);

/** An address in the parts a one-line label asks for: "City, ST", "City, ST
 *  ZIP", or the full street address. Null when it asks for no more than one
 *  field gives, or a part is unknown. */
function composedAddress(label: string, profile: UserApplicationProfile): string | null {
  const loc = profileFacts(profile).location;
  const city = isHigh(loc.city) ? loc.city.value : null;
  const region = isHigh(loc.region) ? loc.region.value.code : null;
  const postal = isHigh(loc.postalCode) ? loc.postalCode.value : null;
  const street = isHigh(loc.street) ? loc.street.value : null;
  const zip = /\b(zip|postal|post ?code)\b/i.test(label);
  // One part of a split address ("Home Address Line 1", "Home Address City",
  // SoFi) is that part; only a label asking for the WHOLE address gets it.
  const part = /\b(line ?\d|apt|apartment|unit|suite|city|state|province|zip|postal|cep|country|street (1|2))\b/i.test(label);
  // The street with the rest asked along: "What is your street address?
  // Please also include your city, state, and zip code." (Crest on Lever,
  // question bank 2026-10-08) got the city line without the street; and
  // "your complete current residential address" (FiscalNote) the city alone.
  const withRest = /\bstreet address\b/i.test(label) && /\b(include|including|with|along with|as well as)\b[^?.]{0,30}\bcity\b/i.test(label);
  if ((withRest || (!part && (/\b(full|complete) (current |home |mailing |street |residential |permanent )*address\b/i.test(label) || /^\s*what is your (current |home |mailing |permanent |residential )*address\b/i.test(label)))) && !/\be-?mail\b/i.test(label)) {
    if (!street || !city) return null;
    // Outside North America the country is part of it ("Torstraße 109,
    // Berlin, 10119" lacked Germany).
    const country = isHigh(loc.country) && loc.country.value.code !== "US" && loc.country.value.code !== "CA" ? loc.country.value.name : null;
    return [street, city, [region, postal].filter(Boolean).join(" "), country].filter(Boolean).join(", ");
  }
  if (/\bcity\b[^?]{0,20}\b(state|province|region)\b/i.test(label)) {
    if (!city || !region || (zip && !postal)) return null;
    return `${city}, ${region}${zip ? ` ${postal}` : ""}`;
  }
  return null;
}

/**
 * Snap `value` onto one of `options`, strictly. Country names get their
 * spelling variants first ("Canada" ↔ "CA", "USA" ↔ "United States of America").
 */
/**
 * A self-ID answer's option, by the self-ID rules first: "Asian" names none
 * of East, South and Southeast Asian, and the general matcher took the
 * shortest of them, "East Asian (inclusive of …)" (Gusto's race list on
 * Greenhouse, live, round 8). Only an answer no option holds goes on to the
 * general matcher.
 */
function demographicSnap(category: FieldCategory, value: string, options: string[]): string | null {
  return closestDemographicOption(category, value, options) ?? (severalHold(value, options) ? null : snapToOption(options, value, category));
}

/** Whether the education row a field answers for (its own, else the main
 *  one: in progress, else the most recent) is finished. */
function rowFinished(profile: UserApplicationProfile, groupIndex: number | null): boolean {
  const edu = profileFacts(profile).education;
  const rows = profile.education ?? [];
  const main = edu.primary;
  const row = groupIndex !== null ? rows[groupIndex] : rows.find((e) => main && (e.school || "").trim() === main.school && (e.degree || "").trim() === main.degree) ?? rows[0];
  if (!row) return false;
  return edu.entries.find((e) => e.school === (row.school || "").trim() && e.degree === (row.degree || "").trim())?.completed === true;
}

/** A degree field asked at one level of study ("Law Degree", "Undergraduate
 *  Degree", "Graduate Degree"), not a row's own Degree list. */
export function degreeAtALevel(label: string): boolean {
  return /\b(law|undergrad\w*|bachelor\w*|graduate|post ?grad\w*|master\w*|doctor\w*|doctoral|ph ?d|mba)\b/i.test(label);
}

export function snapToOption(options: string[], value: string, category: FieldCategory): string | null {
  if (category === "school") return snapSchool(options, value);
  const direct = pick(options, value);
  if (direct) return category === "country" || category === "location" ? sameplaceInEnglish(options, direct) : direct;
  if (category === "country" || category === "phoneCountryCode") {
    const want = countryFromName(value);
    if (want) {
      const hit = onePlaceOption(options.filter((o) => countryFromName(o.replace(/\s*\(.*\)\s*$/, ""))?.code === want.code));
      if (hit) return hit;
    }
  }
  // "What is your location?" over a list of countries (Hermeus on Lever, live
  // 2026-10-03): the country the place is in.
  if (category === "location") {
    const code = placeOf(value).country;
    if (code) {
      const hit = onePlaceOption(options.filter((o) => countryFromName(o.replace(/\s*\(.*\)\s*$/, ""))?.code === code));
      if (hit) return hit;
    }
  }
  // A major the list does not carry ("Mechatronics Engineering" among Computer
  // Science / Computer Engineering / … / Other, Palantir on Lever): "Other" is
  // true, a sibling discipline is not.
  if (category === "fieldOfStudy") {
    // "Computing" is Computer Science (Imperial's MSc in Computing got "Other"
    // on Palantir's major list, live 2026-10-03).
    if (/^(computing|informatics|informatique|informatik)$/i.test(value.trim())) {
      const cs = options.filter((o) => /^computer science$/i.test(o.trim()));
      if (cs.length === 1) return cs[0];
    }
    const other = options.filter((o) => /^other(\s*\(.*\))?\W*$/i.test(o.trim()));
    if (other.length === 1) return other[0];
    // "Other (Technical)" and "Other (Non-Technical)" (SpaceX's Discipline,
    // live 2026-10-03: "Software Engineering" went to the AI): by the major.
    if (other.length > 1) {
      const wanted = TECHNICAL_FIELD.test(value) ? /\(\s*technical\s*\)/i : /\(\s*non[\s-]?technical\s*\)/i;
      const qualified = other.filter((o) => wanted.test(o));
      if (qualified.length === 1) return qualified[0];
    }
  }
  return null;
}

/**
 * The one option for a place. Several name it, each in its own language
 * ("Canada (English)", "Canada (French)": a company's consent policies on
 * Jobvite): the English one, the language the application is filled in.
 */
function onePlaceOption(hits: string[]): string | null {
  if (hits.length === 1) return hits[0];
  const english = hits.filter((o) => /\(\s*english\s*\)\s*$/i.test(o));
  return english.length === 1 ? english[0] : null;
}

/** The chosen place's English option, when the list has it in several
 *  languages: "Berlin, Germany" matched "Germany (German)" by its words. */
function sameplaceInEnglish(options: string[], chosen: string): string {
  const base = (o: string): string => o.replace(/\s*\(.*\)\s*$/, "").trim().toLowerCase();
  const siblings = options.filter((o) => base(o) === base(chosen));
  return siblings.length > 1 ? (onePlaceOption(siblings) ?? chosen) : chosen;
}

/** An option for every place a list does not name, once its "(Language)" is
 *  off: "All Locations", "All Other Locations", "Rest of World". Never a bare
 *  "Other", which a list may mean otherwise. */
const ANY_PLACE = /^(all( other)? (locations|countries|regions)|(the )?rest of (the )?world|other (locations|countries|regions)|global|worldwide|international)$/i;

/**
 * Where the applicant lives, from a list that does not name their country:
 * its option for every other place. Nutanix's data consent on Jobvite lists
 * one policy, "All Locations (English)", under "Location of Residence and
 * Language" (live 2026-10-17): nothing was chosen, so the consent and the
 * application behind it never opened. Only for a known country no option
 * names, and only where the question asks where they live.
 */
function anyPlaceOption(options: string[], value: string, label: string): string | null {
  if (!/\bresiden(ce|t|cy)\b|\breside\b|\bwhere (do )?you (currently )?live\b/i.test(label)) return null;
  const code = countryFromName(value)?.code ?? placeOf(value).country;
  if (!code) return null;
  if (options.some((o) => countryFromName(o.replace(/\s*\(.*\)\s*$/, ""))?.code === code)) return null;
  return onePlaceOption(options.filter((o) => ANY_PLACE.test(o.replace(/\s*\(.*\)\s*$/, "").trim())));
}

/** A major in engineering, the sciences or computing. */
const TECHNICAL_FIELD =
  /\b(engineering|computer|computing|software|mathematics|math|physics|chemistry|biochemistry|biology|statistics|data|informatics|information (technology|systems)|robotics|mechatronics|electronics|astronomy|geology)\b|\b(applied|natural|physical|computer|data|materials|earth|environmental) sciences?\b/i;

function coerceToKind(value: string, kind: AnswerKind): string | null {
  const v = value.trim();
  if (kind === "number") {
    // "$120,000" → "120000"; "120k" → "120000"; "3 years" → "3"; two
    // different numbers (a range) → refuse rather than pick one.
    // A currency amount with periods between thousands ("€120.000", a Berlin
    // salary) is one number: Mindex's Salary Range got 120 (Workable bank,
    // 2026-10-05). Only beside a currency: "3.850" alone may be a GPA.
    const european = /[€$£]|\b(eur|usd|cad|gbp|chf)\b/i.test(v) && /^\D*\d{1,3}(\.\d{3})+(,\d{1,2})?\D*$/.test(v);
    const flat = european ? v.replace(/\./g, "").replace(/,(\d{1,2})\D*$/, ".$1") : v.replace(/,/g, "");
    const nums = flat.match(/\d+(?:\.\d+)?/g) ?? [];
    if (nums.length !== 1) return null;
    const k = /(\d+(?:\.\d+)?)\s*k\b/i.exec(flat);
    return k ? String(Math.round(parseFloat(k[1]) * 1000)) : nums[0];
  }
  // A URL input rejects a link without a scheme ("linkedin.com/in/…" was
  // written into Superhuman's, live 2026-10-03): the page would not submit.
  if (kind === "url") return valueFitsKind(v, kind) ? (/^https?:\/\//i.test(v) ? v : `https://${v}`) : null;
  if (kind === "phone" || kind === "email" || kind === "date") {
    return valueFitsKind(v, kind) ? v : null;
  }
  return v;
}

/**
 * A visible minority (Canada's "racialized person") from the stated race:
 * any race but White is one, White is not; Indigenous peoples are counted
 * apart, and two or more races are not decided. Nothing stated, or declined:
 * the list's own decline option.
 */
function visibleMinorityOption(profile: UserApplicationProfile, options: string[]): string | null {
  const race = (profile.eeo?.race ?? "").toLowerCase();
  const hispanic = /^yes\b/i.test((profile.eeo?.hispanicLatino ?? "").trim());
  const decline = options.find((o) => /\b(prefer not|decline|do not wish|don.t wish|choose not|rather not)\b/i.test(o)) ?? null;
  if ((!race.trim() && !hispanic) || /\b(prefer not|decline|not (to )?(say|disclose|answer)|do not wish)\b/.test(race)) return decline;
  const minority = hispanic || /\b(black|african|asian|hispanic|latin|middle eastern|arab|north african|pacific islander|native hawaiian)\b/.test(race)
    ? true
    : /\b(white|caucasian|european)\b/.test(race) && !/\b(indigenous|first nations|metis|inuit|aboriginal|american indian|alaska native|native american|two or more|mixed|multiracial)\b/.test(race)
      ? false
      : null;
  if (minority === null) return null;
  // "Nonvisible minority" beside it (Wattpad): someone LGBTQ+ or with a
  // disability may be one, so "I don't identify as a minority (Visible or
  // Nonvisible)" is not theirs to be given.
  const eeo = profile.eeo;
  const nonvisible =
    /\b(gay|lesbian|bisexual|queer|pansexual|asexual)\b/i.test(eeo?.sexualOrientation ?? "") ||
    /\btrans/i.test(eeo?.genderIdentity ?? "") ||
    /^yes\b/i.test((eeo?.disabilityStatus ?? "").trim());
  if (!minority && nonvisible && options.some((o) => /\bnon ?-?visible minorit/i.test(o))) return null;
  const yes = options.find((o) => /^yes\b/i.test(o.trim())) ?? options.find((o) => /^visible minorit/i.test(o.trim()));
  const no = options.find((o) => /^no\b/i.test(o.trim())) ?? options.find((o) => /\b(not|don.t|do not) identify as (a )?(visible )?minorit/i.test(o));
  return (minority ? yes : no) ?? null;
}

/**
 * A date control (dateControl.ts) takes only a whole date in its own format:
 * the value is re-emitted in that format, or the field stays blank when it
 * lacks a part (a graduation YEAR for a day-precise picker).
 */
export function resolveField(input: FieldResolveInput): FieldResolution {
  const r = resolveFieldValue(input);
  const dateFormat = dateFormatFor(input.el, input.signals.typeHint, input.signals.placeholder);
  if (!dateFormat) {
    // A signature box that shows no format takes today month first, as the
    // US forms that ask for one write it (Lever's disability form "Date").
    if (input.category === "signatureDate" && r.value) return { ...r, value: fitDate(r.value, "MM/DD/YYYY") ?? r.value };
    return r;
  }
  if (r.value === null) return { ...r, dateFormat };
  const value = fitDate(r.value, dateFormat);
  if (value === null) {
    // The profile knows the date to the month or year only: the missing day
    // is no more the AI's to invent than ours (Ramp's "Pick date...", live
    // 2026-10-03, was sent to it).
    return { value: null, kind: r.kind, source: "none", deviceAbstained: true, rule: "wrong-kind:partial-date", dateFormat };
  }
  return { ...r, value, dateFormat };
}

/** A box whose own label only acknowledges answers the question it sits
 *  under: "Acknowledge/Confirm" under the legend "Personal Information Policy"
 *  (Everpure on Greenhouse, live 2026-10-17) went to the AI and was dropped.
 *  Read with that question, the arbitration, recording and authorship guards
 *  see it too. Only its own group's legend: the text nearest the box can be a
 *  description ("Candidate AI Responsible Use Policy" under Twilio's pledge,
 *  question bank) or the question before it. */
const BARE_ACKNOWLEDGEMENT =
  /^\s*(i )?(acknowledged?|confirm(ed)?|agreed?|understood|understand)(\s*([/&,]|\band\b|\bor\b)\s*(acknowledged?|confirm(ed)?|agreed?|understood|understand))?[\s*.]*$/i;

function legendOf(el: HTMLElement | undefined): string {
  const legend = el?.closest("fieldset")?.querySelector(":scope > legend");
  return legend ? cleanText(legend.textContent) : "";
}

function resolveFieldValue(input: FieldResolveInput): FieldResolution {
  const { profile, category, control, signals } = input;
  const legend = control.controlType === "checkbox" && BARE_ACKNOWLEDGEMENT.test(input.label) ? legendOf(input.el) : "";
  const label = legend ? `${legend} ${input.label}` : input.label;
  const options = control.options;
  const kind = answerKindOf({
    controlType: control.controlType,
    inputType: signals.typeHint,
    label,
    helpText: signals.nearby,
    placeholder: signals.placeholder,
    options,
    multi: control.multi,
  });
  const none = (deviceAbstained = false, rule?: string): FieldResolution => ({ value: null, kind, source: "none", deviceAbstained, rule });
  if (!profile) return none();
  if (kind === "file" || kind === "password" || PASS_THROUGH.has(category)) {
    const v = resolveAnswerWithAdapter(input.adapter, category, profile, control, input.fillEEO, input.el);
    return { value: v, kind, source: v === null ? "none" : "category", deviceAbstained: false };
  }
  // Canada's employment-equity "racialized person" / "visible minority"
  // question is about race: "Do you self-identify as a racialized person?
  // …Latin American/Hispanic…" (eqbank on Lever, question bank 2026-10-08)
  // was read as the Hispanic question, No for a Black and two Asian
  // applicants. Wattpad's list names it in its options. Only the label's
  // first question counts: eqbank's disability question goes on "Visible
  // Minority Persons - People experience disabilities…".
  const minorityAsked = /\bracialized\b|\bvisible minorit(y|ies)\b/i.test(label.split("?")[0]) || (options ?? []).some((o) => /^visible minorit/i.test(o.trim()));
  if (minorityAsked && options && options.length > 1) {
    const v = visibleMinorityOption(profile, options);
    // A rule's answer, selected on its own evidence: Wattpad's list is
    // otherwise an unknown field, and stayed unselected live (2026-10-08).
    return v === null ? none(true, "eeo:visible-minority-unknown") : { value: v, kind, source: "question", deviceAbstained: false, rule: "eeo:visible-minority" };
  }

  // 2. Question shapes. EEO answers are the user's own words and are matched
  //    by demographicMatch, never computed. A repeating employment/education
  //    row ("experience[1][start]") is transcription of THAT row, never a
  //    screening question.
  let q: QuestionResult = null;
  // groupIndex alone is not proof of a repeating row: it is parsed from ids,
  // and Ashby's radio ids end "-labeled-radio-0". Only the row categories count.
  // A radio group is never a row's date or school control: Superhuman's "When
  // is your expected graduation date?" radios (ids "…-labeled-radio-0") were
  // treated as a row's graduation year and never reached the question resolver.
  const isRadios = control.controlType === "radioGroup" || control.controlType === "ariaRadioGroup";
  const inRepeatingRow = control.groupIndex !== null && control.groupIndex !== undefined && ROW_CATEGORIES.has(category) && !isRadios;
  if (!input.sensitive && !inRepeatingRow) {
    const question: QuestionInput = {
      label,
      helpText: signals.nearby,
      controlType: control.controlType,
      inputType: signals.typeHint,
      placeholder: signals.placeholder,
      options,
      category,
      kind,
    };
    q = resolveQuestion(question, profileFacts(profile), profile, resolveContext);
    if (q?.status === "abstain") return none(q.blockBackend === true, q.rule);
  }

  let value: string | null;
  let source: FieldResolution["source"];
  let rule: string | undefined;
  if (q?.status === "answer" && q.confidence === "high") {
    value = q.value;
    source = "question";
    rule = q.rule;
  } else {
    value = resolveAnswerWithAdapter(input.adapter, category, profile, control, input.fillEEO, input.el);
    source = "category";
  }
  // An education or employment row's City / State / Country belongs to the
  // school or the employer, never to the applicant's address (Paylocity's
  // "educationHistory.city.0" got the applicant's city, live 2026-10-03).
  if (source === "category" && APPLICANT_ADDRESS.has(category) && HISTORY_ROW.test(`${signals.nameAttr} ${signals.idAttr}`)) {
    return none(true, "row:not-applicant-address");
  }
  // "Legal Name (if different than above)" (Cloudflare on Greenhouse, live
  // 2026-10-05): the profile has one name, so it is never different.
  if (source === "category" && (category === "fullName" || category === "firstName" || category === "lastName") && /\bif (it is |its )?different\b/i.test(label)) {
    return none(true, "name:if-different");
  }
  // "What is your preferred first name and last name? If your legal first and
  // last name is your preferred name, you do not need to respond." (Zoox on
  // Lever, question bank 2026-10-08) got the first name alone. The profile
  // has one name: nothing to add.
  if (source === "category" && (category === "fullName" || category === "firstName" || category === "lastName") && /\b(do not|don ?t|no) need to (respond|answer|fill)\b|\bleave (this|it) blank\b/i.test(label) && /\bpreferred\b/i.test(label)) {
    return none(true, "name:preferred-same");
  }
  // The whole name asked under a first-name label: "Legal Full Name (First
  // Name, Middle Name & Last Name)" (ERG on Lever, question bank 2026-10-08)
  // got the first name alone.
  if (source === "category" && (category === "firstName" || category === "lastName") && (/\bfull (legal )?name\b/i.test(label) || (/\bfirst\b/i.test(label) && /\blast\b/i.test(label) && !/\bpreferred\b/i.test(label)))) {
    const full = [profile.firstName, profile.lastName].map((s) => (s ?? "").trim()).filter(Boolean).join(" ");
    if (full) value = full;
  }
  // "In what City, State and Zip are you currently residing?" (Box) and
  // "…city and state… (e.g. San Jose, CA)" (Zscaler) got the city alone;
  // "What is your current full address?" (Riot) got no street (question bank
  // 2026-10-05). The parts the label asks for, in one line.
  if (source === "category" && (kind === "text" || kind === "longText") && (category === "addressCity" || category === "location" || category === "addressStreet")) {
    // A box read as the whole location whose label names an address asks
    // for all of it: "Current Mailing Address" (Arcesium on Greenhouse,
    // live, round 7) got "Seattle, WA" from a profile with the street.
    // (Read as the street, an "Address" box is a split form's first line.)
    const whole = category === "location" && /^\s*(current |home |mailing |permanent |residential |physical |postal )+address\s*[*:]?\s*$/i.test(label);
    const composed = composedAddress(whole ? `Full ${label}` : label, profile);
    if (composed) value = composed;
  }
  // "Please type in a language that you speak" is one row of a language list,
  // its level asked beside it (SmartRecruiters bank, round 7): it got "English
  // (Native), French (Professional)". The first language, without its level.
  if (source === "category" && category === "languages" && value && kind === "text" && /\b(a|one|1st|first|primary) language\b/i.test(label) && !/\blanguages\b/i.test(label)) {
    const first = value.split(/[,;\n]/)[0].replace(/\s*\([^)]*\)\s*$/, "").trim();
    if (first) value = first;
  }
  // A multi-select of languages whose options are not read yet: Greenhouse's
  // react-select opens only for a real click, and a harvest reads 60 of its
  // 539 (Gallup, live, round 7), so "Korean (Native), English (Fluent)" was
  // typed whole and matched nothing. Each stated language by name, one chip
  // each; none stated at a beginner's level, none the question leaves out
  // ("in addition to English").
  if (source === "category" && category === "languages" && value && control.multi && !(options && options.length)) {
    const left = /\b(?:in addition to|other than|besides|apart from|aside from|except|excluding|beyond)\s+([a-z]+)\b/i.exec(label)?.[1]?.toLowerCase() ?? null;
    const names = value
      .split(/[,;\n]+/)
      .map((l) => l.trim())
      .filter((l) => l && !/\((basic|beginner|elementary|limited|a1|a2)\b/i.test(l))
      .map((l) => l.replace(/\s*\(.*$/, "").trim())
      .filter((l) => l && l.toLowerCase() !== left);
    if (names.length === 0) return none(false, "languages:none-but-excluded");
    // Read off the profile, as a school or a degree is: filled on device
    // (with the AI down it was never written at all).
    return { value: [...new Set(names)].join(", "), kind, source: "question", rule: "languages:by-name", deviceAbstained: false };
  }
  // "What are your salary expectations (hourly)?" (StackAdapt, question bank
  // 2026-10-05) got "$185,000": a pay figure only in the unit it was asked in.
  if (source === "category" && category === "salary" && value && (kind === "text" || kind === "number" || kind === "longText")) {
    const unitOf = (t: string): string | null =>
      /\b(hourly|per hour|an hour|hour ?rate)\b|\/ ?(h|hr|hour)\b/i.test(t) ? "hour" : /\b(monthly|per month|a month)\b|\/ ?(mo|month)\b/i.test(t) ? "month" : /\b(annual|annually|yearly|per year|a year|per annum)\b|\/ ?(yr|year)\b/i.test(t) ? "year" : null;
    const asked = unitOf(label);
    const given = unitOf(value) ?? "year";
    if (asked && asked !== given) return none(true, "salary:other-unit");
  }
  // "Secondary Major" (Jane Street, question bank 2026-10-05) got the major:
  // the profile names one field of study.
  if (source === "category" && category === "fieldOfStudy" && (/\b(secondary|second|double|dual|additional) (major|field|concentration)\b/i.test(label) || (/\bminors?\b/i.test(label) && !/\bmajor\b/i.test(label)))) {
    return none(true, "major:no-second");
  }
  // "University Email Address" (Jane Street, question bank 2026-10-05) got
  // the personal one: only an academic address is a university email.
  if (source === "category" && category === "email" && /\b(university|school|student|college|academic|campus|\.edu)\b[^?]{0,20}\be-?mail\b/i.test(label) && !/@[^@\s]+\.(edu|ac\.[a-z]{2,3}|edu\.[a-z]{2})$/i.test((value ?? "").trim())) {
    return none(true, "email:not-academic");
  }
  // "Alternate Email" (Duolingo, live 2026-10-05) got the email again: the
  // profile holds one email and one phone.
  if (source === "category" && (category === "email" || category === "phone") && /\b(alternate|alternative|secondary|additional|backup|second|other)\b/i.test(label)) {
    return none(true, "contact:no-second");
  }
  // Address Line 2 is the unit ("app. 3" of "4520 rue Saint-Denis, app. 3"),
  // never a copy of line 1 (Pinpoint got "1 Washington Sq" twice, live
  // 2026-10-03). No unit: blank, and nothing for the AI to invent.
  if (category === "addressStreet" && value && isAddressLine2(label, signals)) {
    const unit = /,\s*((?:apt|app|appt|apartment|unit|suite|ste|bureau|#)\b.*)$/i.exec(value);
    if (!unit) return none(true, "address-line2:none");
    value = unit[1].trim();
  }
  if (
    (category === "currentCompany" || category === "currentTitle") &&
    source === "category" &&
    control.controlType !== "checkbox" &&
    (control.groupIndex ?? null) === null
  ) {
    const adjusted = employmentForLabel(category, label, profile, value);
    if (adjusted === null) return none(true, "employment:not-current");
    if (adjusted !== undefined) value = adjusted;
  }
  // A self-ID answer never goes in a long-text box that only mentions the
  // topic: "Other than your ethnicity, gender, and disability status (survey
  // below), is there anything you would like to share with us, or
  // information to help us accommodate you?" (1Password on Ashby, live,
  // round 8) got "Female".
  if (input.sensitive && kind === "longText" && (isProsePrompt(label) || /\bother than\b/i.test(label))) return none(true, "eeo:not-asked-here");
  // A follow-up for those who chose another option ("If you selected
  // 'Other', you can describe your gender in your own words below", Jane on
  // Ashby, live, round 8, got "Non-binary" beside the list that held it).
  if (input.sensitive && /^\W*(?:optional\W*)?if (?:you (?:selected|chose|answered|checked|picked|indicated|said)|your (?:answer|selection|choice|response)|other|not listed)\b/i.test(label)) return none(true, "eeo:follow-up");
  if (input.sensitive) {
    const refined = refineGenderIdentity(category, label, options, profile);
    if (refined !== undefined) value = refined;
  }
  // A veteran in the family is not the applicant's own service: "Are you a
  // spouse or domestic partner to a veteran?" got "No" from "I have never
  // served" (Justworks, live 2026-10-17). The profile holds no answer (so a
  // decline where the list has one).
  if (input.sensitive && category === "eeoVeteran" && /\b(spouse|domestic partner|partner|family member|parent|child|dependents?|wife|husband|widow\w*)\b/i.test(label)) value = null;
  // Veteran status: an answer settles only the options it actually claims
  // ("not a protected veteran" is not "never served"); else left for the user.
  if (input.sensitive && category === "eeoVeteran" && value?.trim() && options?.length) {
    const veteran = veteranOption(value, label, options);
    if (veteran === null) return none(false, "eeo:veteran-not-settled");
    if (veteran !== undefined) value = veteran;
  }
  // Race and Hispanic origin asked in one list: a Hispanic applicant's answer.
  if (input.sensitive && category === "eeoRace" && options?.length) {
    const hispanic = hispanicRaceOption(profile.eeo?.hispanicLatino ?? "", options);
    if (hispanic === null) return none(false, "eeo:race-hispanic-unsettled");
    if (hispanic !== undefined) value = hispanic;
  }
  // A demographic question the profile holds no answer for: decline (default).
  if ((value === null || !value.trim()) && input.sensitive) {
    const decline = eeoDecline(category, control.controlType, options);
    if (decline) return { value: decline, kind, source: "question", rule: "default:eeo-decline", deviceAbstained: false };
  }
  if (value === null || !value.trim()) return none();

  // A one-line fact (a company, a title, a city) never answers a PROSE prompt
  // in a long-text box: "Let the company know about your interest working
  // there" classified as currentCompany on the word "company", and the
  // employer's name was written into it (SmartRecruiters, live 2026-10-03).
  // A label that actually asks for the name ("What is the name of…") is
  // answered by the question resolver above, before this point.
  if (source === "category" && kind === "longText" && SINGLE_LINE_FACTS.has(category) && isProsePrompt(label)) {
    return none(false, "wrong-kind:prose-prompt");
  }
  // A question of another shape whose words name a fact is not asking for it:
  // "How are you using AI today in your current role?" got the title
  // (Airtable), "Are you located in and willing to work in the location where
  // the job is posted?" the city and "Are your salary expectations … within the
  // posted band?" the salary (CircleCI); question bank 2026-10-05.
  if (source === "category" && (kind === "text" || kind === "longText") && asksAnotherThing(label, category)) {
    return none(false, "wrong-kind:other-question");
  }
  // "What is your preferred office location?" (Hudl, question bank
  // 2026-10-05) asks which of the company's offices: the home city was typed.
  // The office CLOSEST to them is a fact of where they live ("Which office hub
  // are you closest to?", Tubi: Seattle).
  if (source === "category" && (category === "location" || category === "addressCity") && /\bpreferred (office|work|working|job)? ?location\b|\bpreferred office\b|\bwhich (of our )?offices?\b/i.test(label) && !/\b(closest|nearest|close to|near)\b/i.test(label)) {
    return none(false, "wrong-kind:office-preference");
  }
  // Where the job will be done is not where one lives now: "If you are
  // accepted for this in-person position, what city will you be working
  // from?" got Toronto from someone who would move (SEP on Lever, question
  // bank 2026-10-08).
  if (source === "category" && (category === "location" || category === "addressCity") && /\b(will|would) you (be )?(work|working|based|located|reporting)( from| out of| in)?\b/i.test(label)) {
    return none(false, "wrong-kind:future-work-place");
  }
  // Experience of one kind is not the work history: "Do you have any SaaS or
  // software sales experience?" (Hootsuite), "…any experience you have within
  // sport?" (Hudl), "…your experience in product marketing" (Wikimedia).
  if (source === "category" && category === "experience" && !GENERAL_EXPERIENCE.test(label)) {
    return none(false, "wrong-kind:experience-of-a-kind");
  }
  // A degree asked at one level ("Law Degree", "Undergradute Degree",
  // "Graduate Degree": Barnes & Thornburg on Ashby, question bank, round 8)
  // is no row's Degree list: a bootcamp's certificate took each list's
  // "Other" there. Decision 96 is for the row's own Degree list. Nor is a
  // degree being obtained one finished ("Can you please confirm the current
  // degree that you are obtaining?", Bosch on SmartRecruiters, bank).
  const asksCurrentDegree = /\b(current(ly)?|obtaining|pursuing|working towards?|in progress)\b/i.test(label);
  if (source === "category" && category === "degree" && value && /^other\b/i.test(value.trim()) && (degreeAtALevel(label) || (asksCurrentDegree && profile && rowFinished(profile, control.groupIndex ?? null)))) {
    return none(false, "degree:level-asked");
  }

  // 4. Gate. Single checkboxes keep their own intent logic (checkboxIntent),
  //    which reads the label; everything else must fit its kind.
  if (control.controlType !== "checkbox") {
    if (kind === "boolean" && optionPolarity(value) === null && !(options && options.length)) {
      return none(false, "wrong-kind:not-yes-no");
    }
    if (kind !== "choice" && kind !== "multiChoice" && kind !== "boolean" && kind !== "text" && kind !== "longText") {
      const coerced = coerceToKind(value, kind);
      if (coerced === null) return none(false, `wrong-kind:${kind}`);
      value = coerced;
    }
  }
  if (options && options.length > 0 && (CONSTRAINED.has(control.controlType) || control.controlType === "combobox")) {
    if (control.controlType === "checkboxGroup" || control.multi) {
      // An answer that IS one option's whole text is that option, commas and
      // all: "Yes, but require relocation" ticked "Yes" too (Vital Lyfe's
      // checkboxes, Ashby bank 2026-10-08).
      const key = (s: string | null): string => (s ?? "").toLowerCase().replace(/\s+/g, " ").trim();
      const said = key(value);
      const whole = options.filter((o) => key(o) === said);
      if (whole.length === 1) return { value: whole[0], kind, source, rule, deviceAbstained: false };
      const parts = value.split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean);
      const hits = parts.map((p) => (input.sensitive ? demographicSnap(category, p, options) : snapToOption(options, p, category)));
      // One option, once: "No, I do not have a disability" splits on its own
      // comma, and both halves land on the same box ("No, I do not have a
      // disability and have not had one in the past" twice over).
      const ok = [...new Set(hits.filter((h): h is string => h !== null))];
      if (ok.length === 0) return none(false, "no-confident-option");
      return { value: ok.join(", "), kind, source, rule, deviceAbstained: false };
    }
    // A list grouped "White: Irish" / "White: Any other White background" (the
    // UK census): the stated group's own "any other", never a subgroup
    // nobody stated (Spotify on Lever, question bank 2026-10-08: "White:
    // Irish" for a German).
    const grouped = input.sensitive && (category === "eeoRace" || category === "eeoHispanic") ? groupedEthnicityOption(value, options) : undefined;
    if (grouped === null) return none(false, "eeo:grouped-subgroup-unknown");
    if (grouped !== undefined) return { value: grouped, kind, source, rule: "eeo:grouped", deviceAbstained: false };
    // A demographic answer in other words ("Female" among Man / Woman, Ashby):
    // the on-device demographic matcher knows the synonyms.
    const snapped =
      (input.sensitive ? demographicSnap(category, value, options) : snapToOption(options, value, category)) ??
      (category === "location" || category === "country" ? anyPlaceOption(options, value, label) : null);
    if (!snapped) return none(false, "no-confident-option");
    value = snapped;
  }
  return { value, kind, source, rule, deviceAbstained: false };
}
