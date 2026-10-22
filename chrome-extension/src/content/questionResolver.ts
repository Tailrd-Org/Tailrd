/**
 * Deterministic question answering: recognizes the SHAPE of a screening
 * question and answers it from derived profile facts (profileFacts.ts),
 * rendered into what the field accepts (one of its real options, or Yes/No,
 * or a number, or a date).
 *
 * Three outcomes, and the difference between the last two is load-bearing:
 *   answer   the shape is recognized and the facts settle it (high confidence)
 *   abstain  the shape is recognized and the facts do NOT settle it. The field
 *            must stay blank: no category fallback, no backend guess. "Are you
 *            authorized to work in the United States?" for a Canadian citizen
 *            is unknown, and the old category path answered it "Yes".
 *   null     not a shape this module knows; the category resolver decides.
 *
 * Pure: no DOM. `today`, the job's country and company are injected.
 */
import type { ControlType, FieldCategory, UserApplicationProfile } from "../shared/types";
import { isBooleanOptionSet, isConsentOption, optionPolarity, type AnswerKind } from "./answerKind";
import {
  CA_PROVINCES as CA_PROVINCES_LIST,
  COUNTRIES,
  KNOWN_CITIES,
  US_STATES as US_STATES_LIST,
  countryByCode,
  countryFromName,
  countryHintForCity,
  regionHintForCity,
  regionsBorder,
  sameMetro,
  DIAL_CODES,
  geoNorm,
  ownKeys,
  regionFromText,
  type Country,
} from "./geo";
import {
  authorizedIn,
  degreeRank,
  isHigh,
  needsSponsorshipIn,
  polarityOf,
  type Confidence,
  type ProfileFacts,
} from "./profileFacts";
import { matchOption } from "./writeEngine";
import { dialCodeOf, phoneCountryName } from "./phoneNumber";
import { deriveFieldOfStudy } from "./fieldMatcher";
import { schoolOffered, snapSchool } from "./schoolMatch";
import { NON_COMPETE, onsiteVerdict, resolveDefault } from "./defaultAnswers";
import { STATEMENT_WORDS, askedOfStatement, sentencesOf } from "./statementText";
import { placeOf } from "./placeMatch";

export interface QuestionInput {
  label: string;
  helpText?: string;
  controlType: ControlType;
  inputType?: string;
  placeholder?: string;
  options?: string[];
  category: FieldCategory;
  kind: AnswerKind;
}

export interface QuestionContext {
  /** ISO code of the country the JOB is in, when the page says. */
  jobCountry: string | null;
  /** The hiring company's name, when known. */
  company: string;
  /** The job's city, when the page says ("San Francisco"). */
  jobCity?: string | null;
  /** Every place the posting lists, when it lists several ("San Francisco, CA"). */
  jobPlaces?: string[] | null;
}

export type QuestionResult =
  | { status: "answer"; value: string; confidence: Confidence; rule: string }
  | { status: "abstain"; rule: string; blockBackend?: boolean }
  | null;

const answer = (value: string, rule: string, confidence: Confidence = "high"): QuestionResult => ({
  status: "answer",
  value,
  confidence,
  rule,
});
/**
 * Abstentions on LEGAL-STATUS facts (work authorization, sponsorship,
 * citizenship, age) also keep the field from the backend: its rule pass
 * answers those unconditionally ("authorized to work?" Yes, "sponsorship?"
 * No, "18 or older?" Yes), so sending them is asking for a guess. Every
 * other abstention leaves the field to the backend's AI (essays, opinions,
 * a skill's years), which is the right tool once it has credits again.
 */
// "region-choice:outside-not-offered": another country's states, the
// applicant's own not among them; any pick is a guess.
const BLOCK_BACKEND_RULES = /^(work-auth|sponsorship|citizenship|age-gate|conditional:does-not-apply|clearance:other-country|graduation:not-enrolled|pursuing:not-enrolled|school-name:not-enrolled|discipline:no-degree-at-level|f1-status|clearance:level-unknown|high-school:not-in-profile|school-schedule:unknown|conditional-follow-up|phone-extension:not-in-profile|region-choice:outside-not-offered|school-level:none|graduation:month-unknown|bot-check)/;
const abstain = (rule: string): QuestionResult => ({ status: "abstain", rule, blockBackend: BLOCK_BACKEND_RULES.test(rule) });

/** Lowercase, accents stripped, apostrophes dropped, punctuation → space. */
export function qnorm(text: string): string {
  return (text || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[’']/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9+]+/g, " ")
    .trim();
}

// ---------------------------------------------------------------------------
// Rendering a computed answer into what the field accepts
// ---------------------------------------------------------------------------

const pickOption = (options: string[], value: string): string | null => matchOption(options, (o) => o, (o) => o, value);

/** Render a computed yes/no. With options: the ONE option of that polarity. */
export function renderBoolean(value: boolean, q: QuestionInput): string | null {
  const opts = (q.options ?? []).filter((o) => o.trim());
  if (opts.length > 0) {
    const same = opts.filter((o) => optionPolarity(o) === value);
    if (same.length === 1) return same[0];
    const bare = same.filter((o) => /^(yes|no)$/i.test(o.trim()));
    return bare.length === 1 ? bare[0] : null;
  }
  if (q.controlType === "checkbox") return value ? "yes" : "no";
  return value ? "Yes" : "No";
}

function booleanResult(value: boolean, q: QuestionInput, rule: string): QuestionResult {
  const v = renderBoolean(value, q);
  return v ? answer(v, rule) : abstain(`${rule}:no-matching-option`);
}

/** A dropdown with no options yet, asked as a yes/no ("Are you…?", "Do you…?"). */
function isYesNoDropdown(q: QuestionInput, n: string): boolean {
  if (q.options && q.options.length) return false;
  if (q.controlType !== "combobox" && q.controlType !== "customDropdown") return false;
  return /^(are|do|will|would|can|could|have|has|is|did) you\b/.test(n);
}

/** The question asks for the boolean, its options (when known) allow it. */
function isBooleanQuestion(q: QuestionInput): boolean {
  if (q.kind === "boolean") return true;
  return isBooleanOptionSet(q.options);
}

// ---------------------------------------------------------------------------
// Country / place named by a question
// ---------------------------------------------------------------------------

// "…the country you’ll be employed in?" (Western Digital, SmartRecruiters
// bank, round 7) too.
const THIS_COUNTRY = /\b(this country|the country (in which|where) (this|the) (job|position|role)|country of employment|the country where you are applying|where this (job|role|position) is located|the country (that )?you(['’]ll| will| would|['’]d) be (employed|working|hired|based) in)\b/i;

/**
 * The country a question asks about, from its own words. "U.S."/"US" only
 * count in capitals: lowercase "us" is the pronoun ("work for us").
 * `"this-country"` = the question points at the job's country.
 */
/** Every country a label names, in no particular order ("the U.S. or Canada"
 *  is both); empty when it names none. */
export function countriesNamedIn(raw: string): string[] {
  const text = raw || "";
  const out = new Set<string>();
  if (/\bU\.?\s?S\.?A?\b(?!\w)/.test(text) || /\bunited states\b/i.test(text) || /(?<!(north|south|latin|central)\s)\bamerica\b/i.test(text)) out.add("US");
  if (/\bcanad(a|ian)\b/i.test(text)) out.add("CA");
  if (/\b(united kingdom|great britain|britain)\b/i.test(text) || /\bUK\b/.test(text)) out.add("GB");
  const n = ` ${qnorm(text)} `;
  for (const c of COUNTRIES) {
    if (c.code === "US" || c.code === "CA" || c.code === "GB") continue;
    if (n.includes(` ${qnorm(c.name)} `)) out.add(c.code);
  }
  return [...out];
}

export function countryNamedIn(raw: string): { code: string } | "this-country" | null {
  const text = raw || "";
  if (/\bU\.?\s?S\.?A?\b(?!\w)/.test(text) || /\bunited states\b/i.test(text) || /(?<!(north|south|latin|central)\s)\bamerica\b/i.test(text)) {
    return { code: "US" };
  }
  if (/\bcanad(a|ian)\b/i.test(text)) return { code: "CA" };
  if (/\b(united kingdom|great britain|britain)\b/i.test(text) || /\bUK\b/.test(text)) return { code: "GB" };
  const n = ` ${qnorm(text)} `;
  for (const c of COUNTRIES) {
    if (c.code === "US" || c.code === "CA" || c.code === "GB") continue;
    if (n.includes(` ${qnorm(c.name)} `)) return { code: c.code };
  }
  if (THIS_COUNTRY.test(text)) return "this-country";
  return null;
}

/** "…in the country that you are located?" (Netlify): the applicant's own country. */
// "the location you currently reside" too (Datacor, question bank 2026-10-05:
// read as the job's country, a Canadian at home was "not authorized" there).
const RESIDENCE_COUNTRY =
  /\b(the )?(country|location|place) (that |where |in which )?you (are |currently )*(located|living|reside|live|based)\b|\byour (current )?country of residence\b|\b(remain|stay) in your current (location|country)\b|\bwhere you (currently )?(live|reside)\b/;

function targetCountry(q: QuestionInput, ctx: QuestionContext, facts?: ProfileFacts): string | null {
  // Examples name no place: "(e.g. U.S. F-1, H-1B, TN…)" on a job in Tokyo
  // (New Relic, question bank 2026-10-05) read as a US question, and a US
  // citizen needed no sponsorship there.
  const named = countryNamedIn(q.label.replace(/\((?:e\.?\s?g\.?|for example|i\.?\s?e\.?|such as)[^)]*\)/gi, " "));
  if (named === "this-country") return ctx.jobCountry;
  if (named) return named.code;
  if (facts && RESIDENCE_COUNTRY.test(qnorm(q.label))) return residenceOf(facts);
  return ctx.jobCountry;
}

/**
 * The job's country as the FORM implies it, for a page that states none: when
 * every work-authorization / sponsorship question that names a country names
 * the same one ("Are you authorized to work in the US?" beside an unscoped
 * "Will you require sponsorship?", RAVE on Workable, live 2026-10-03), the
 * employer is asking about the country the job is in.
 */
export function formCountryHint(labels: string[]): string | null {
  const found = new Set<string>();
  for (const label of labels) {
    const n = qnorm(label);
    if (!WORK_RIGHT.test(n) && !SPONSOR.test(n)) continue;
    const c = countryNamedIn(label);
    if (c && c !== "this-country") found.add(c.code);
  }
  return found.size === 1 ? [...found][0] : null;
}

const residenceOf = (facts: ProfileFacts): string | null =>
  isHigh(facts.location.country) ? facts.location.country.value.code : null;

// ---------------------------------------------------------------------------
// Shapes
// ---------------------------------------------------------------------------

// "…have eligibility to work in the US?" (Enfos), "…legally eligible to be
// employed in the United States?" (Saalex), "…autorisés à occuper un emploi
// au Canada?" (Mila); Workable bank, 2026-10-05: all three blank.
const WORK_RIGHT =
  /\b(authori[sz]ed|eligible|entitled|permitted|allowed|legally able|legal right|right|permission) to (legally |lawfully )?work\b|\bwork authori[sz]ation\b|\bwork permit\b|\b(legally|lawfully) (work|be employed|employed)\b|\bauthori[sz]ation to work\b|\bwork (legally|lawfully)\b|\b(eligible|authori[sz]ed|permitted|allowed) to (legally |lawfully )?(begin|start|commence|accept|take up) (employment|work)\b|\beligible for employment\b|\beligibility to (legally |lawfully )?work\b|\b(eligible|authori[sz]ed|permitted) to be employed\b|\bautoris\w* a (travailler|occuper un emploi)\b|\bpermis de travail\b/;

/** A sponsorship need ruled out: "do not require…", "without the need for
 *  sponsorship", "no sponsorship". "No, I need sponsorship now" is no such
 *  thing: its "No" answers the work right. */
const NEGATED_NEED =
  /\b(do not|does not|dont|doesnt|will not|wont|not|never|without)( any| the need for| need for| needing| requiring)? (visa |immigration |employer |employment |company )?(require|requires|need|needs|sponsor\w*)\b|\bno (visa |immigration |employer |employment )?sponsor\w*/;

/** "By selecting 'Yes,' you confirm that you do not require Visa Sponsorship"
 *  (Peloton, question bank 2026-10-05): the label says what Yes means. */
const YES_MEANS =
  /\bby (selecting|choosing|clicking|checking|answering|marking|ticking) yes\b (you|i) (confirm|certify|acknowledge|attest|agree|declare|state|represent|affirm)s? (that )?(.+)$/;

/** "Are you able to work…" is a work RIGHT question only when it names a
 *  country and no arrangement: "able to work from our Kepler office" (Lever,
 *  live 2026-10-03) is about the office, and was answered as authorization. */
function isAbleToWorkInCountry(n: string, raw: string): boolean {
  if (!/\bable to work\b/.test(n)) return false;
  if (/\b(office|onsite|on site|in person|remote|remotely|hybrid|weekends?|nights?|shifts?|overtime|hours|travel|commute|schedule|full time|part time|days a week)\b/.test(n)) return false;
  const c = countryNamedIn(raw);
  return c !== null;
}
/** "What Duolingo sponsored conferences have you attended?" (question bank
 *  2026-10-05) is about conferences, not a visa. */
// "…require TWG Global to file a petition or application for employment-based
// status on your behalf…" (Workable bank, 2026-10-05) is sponsorship too.
// "require sponorship" (sic, Cohere on Ashby, 2026-10-08) is still sponsorship.
const SPONSOR = /\bsponsor(ship|ing)?\b|\bspon(or|ser)ship\b|\bsponsored\b(?! (conferences?|events?|programs?|programmes?|hackathons?|organi[sz]ations?|communit(y|ies)|groups?|clubs?|scholarships?|teams?|content|posts?)\b)|\b(visa|immigration) (status|support|assistance|transfer)\b|\bh ?1 ?b\b|\bfile (a |an )?(visa |immigration )?petition\b|\bemployment ?based (immigration )?status\b/;
/** Asks whether sponsorship is NEEDED ("will you require / do you need … sponsorship"). */
const REQUIRES_SPONSOR = /\b(require|requires|requiring|need|needs|needing)\b[^?]{0,60}\b(sponsor|petition)/;
/** Asks for the work RIGHT itself ("are you legally authorized…", "do you have the right to work…"). */
const ASKS_RIGHT =
  /\b(are|is) (you|the applicant)\b[^?]{0,25}\b(authori[sz]ed|eligible|entitled|permitted|allowed|legally able)\b|\bdo you (have|hold|possess)\b[^?]{0,25}\b(right|authori[sz]ation|permit)\b/;
/** Asks whether the work right is NEEDED ("do you require work authorization?",
 *  "will you need a work permit…"): the inverse of having it. */
// ("…require a visa to work in the United States?", Circle Logistics on
// SmartRecruiters, round 7: blank.)
const NEEDS_RIGHT =
  /\b(do|does|will|would|shall) you\b.{0,32}\b(require|need)\b.{0,30}\b(work authori[sz]ation|authori[sz]ation to work|work permit|work visa|employment authori[sz]ation|a visa to (legally |lawfully )?work)\b/;
/** Asks WHICH sponsorship or visa, not whether: no profile answer states it. */
const SPONSOR_TYPE = /\b(what|which) (type of |kind of |form of )?(visa )?(sponsorship|visa|work permit)\b|\b(type|kind|form) of (visa |work )?(sponsorship|visa|permit)\b/;
// Up to two kinds before it: "without employment visa sponsorship" (OnLogic,
// Workable bank 2026-10-05) read as a plain work-right question; "without
// required sponsorship now or in the future" (M3USA, SmartRecruiters bank,
// round 7) too, and the H-1B and OPT holders said Yes.
const WITHOUT_SPONSOR = /\bwithout (the )?(need (for|of) |needing |requiring |requirement (for|of) |required )?(any )?(current or future )?((visa|employer|employment|immigration|company) ){0,2}sponsor/;
/** The applicant's own sentence states the right ("I am legally authorized to
 *  work in the United States and will not require visa sponsorship", DISA
 *  Technologies on Workable, 2026-10-05): Yes affirms all of it. */
const STATES_RIGHT = /^i am\b[^.?]{0,30}\b(authori[sz]ed|eligible|entitled|permitted|allowed|legally able)\b/;
/** Asks about now only: "currently", nothing about later. */
const NOW_ONLY = (n: string): boolean =>
  /\b(now|currently|at this time|presently)\b/.test(n) && !/\b(future|later|eventually|ever|at any (point|time)|going forward|ongoing)\b/.test(n);

/** A lasting right: "permanent work authorization", "without restriction(s)". */
const PERMANENT_RIGHT =
  /\bpermanent(ly)?\s+(work\s+)?(authori[sz]ation|authori[sz]ed|right|eligib\w*)\b|\bpermanent(ly)? (authori[sz]ed|eligible|allowed|permitted) to work\b|\bwithout (any )?restrictions?\b|\bunrestricted\b|\bwith no restrictions?\b/;

/**
 * "Have you held H-1B status, or had an H-1B petition approved on your behalf
 * in the past 6 years?" (Twitch, live 2026-10-03) asks about the PAST, and was
 * answered as "will you need sponsorship?" (Yes, for a Canadian not authorized
 * in the US). The profile's own statement says H-1B → Yes; a statement naming
 * no US visa at all → No (the unencumbered default); any other US visa
 * (F-1, OPT, TN, L-1…) → the applicant's to answer.
 */
function resolveH1bHistory(q: QuestionInput, n: string, profile: UserApplicationProfile): QuestionResult {
  if (!/\bh ?1 ?b\b/.test(n) || !/\b(have|had|did|were) you\b[^?]*\b(held|had|been|filed|approved|registered|selected|petition)\w*/.test(n)) return null;
  if (!isBooleanQuestion(q)) return abstain("h1b-history:not-yes-no");
  const stated = (profile.workAuthorization || "").toLowerCase();
  if (/\bh-?1 ?b\b/.test(stated)) return booleanResult(true, q, "h1b-history:stated");
  if (/\b(f-?1|j-?1|m-?1|opt|cpt|stem opt|tn|l-?1|o-?1|e-?3|h-?4|ead|green card|visa)\b/.test(stated)) return abstain("h1b-history:other-us-visa");
  return booleanResult(false, q, "default:no-h1b-history");
}

/**
 * "After the OPT, are you eligible for a 24-month OPT extension or are
 * currently in a 24-month OPT extension based upon a degree … in Science,
 * Technology, Engineering, or Mathematics?" (Duolingo, live 2026-10-05: a US
 * citizen got Yes, read as "are you in the US?"). A stated STEM OPT: Yes. A
 * statement naming no U.S. student status (a citizen, an H-1B): No. Plain
 * OPT, or F-1: the applicant's to answer.
 */
function resolveOptExtension(q: QuestionInput, n: string, profile: UserApplicationProfile): QuestionResult {
  if (!/\b(stem )?opt extension\b|\b24 month opt\b|\bstem opt\b/.test(n)) return null;
  // "Will you require sponsorship (e.g., H-1B, E-3, TN, O-1, STEM OPT…)?"
  // (DoorDash, live 2026-10-05): STEM OPT is an example there, and the
  // question is the sponsorship one.
  if (REQUIRES_SPONSOR.test(n)) return null;
  if (!isBooleanQuestion(q)) return abstain("opt-extension:not-yes-no");
  const stated = (profile.workAuthorization || "").toLowerCase();
  if (/\bstem opt\b/.test(stated)) return booleanResult(true, q, "opt-extension:stated");
  if (/\b(f-?1|opt|cpt|student visa|j-?1)\b/.test(stated)) return abstain("opt-extension:unknown");
  if (!stated.trim()) return abstain("opt-extension:unknown");
  return booleanResult(false, q, "opt-extension:no-student-status");
}

/**
 * Whether a sponsorship the applicant needs is needed NOW. Authorized today
 * through a temporary EAD (OPT, a work permit), the need is later; an H-1B,
 * TN or L-1 moves to a new employer only with a new petition, and someone not
 * authorized at all needs it from the start. A label that counts the
 * applicant's own status as sponsorship ("(e.g., H-1B, E-3, TN, O-1, STEM
 * OPT…)", DoorDash, live 2026-10-05) makes it now: a STEM OPT holder needs the
 * new employer's paperwork from day one.
 */
function sponsorshipNeededNow(n: string, facts: ProfileFacts, profile: UserApplicationProfile, country: string | null, residence: string | null): boolean {
  const a = authorizedIn(facts.workAuth, country, residence);
  const stated = (profile.workAuthorization || "").toLowerCase();
  const own = /\bstem opt\b/.test(stated) ? /\bopt\b/ : /\bopt\b/.test(stated) ? /(?<!\bstem )\bopt\b/ : /\bcpt\b/.test(stated) ? /\bcpt\b/ : /\bead\b/.test(stated) ? /\bead\b/ : null;
  const temporary = isHigh(a) && a.value === true && /\b(opt|ead|cpt|work permit|pgwp|open work permit)\b/.test(stated) && !/\b(h-?1 ?b|tn|l-?1|e-?3|o-?1)\b/.test(stated);
  return !temporary || Boolean(own?.test(n));
}

/** Visa names: as the applicant states them, and as an option names them. */
const VISA_NAMES: [RegExp, RegExp][] = [
  [/\bh-?1 ?b\b/, /\bh-?1 ?b\b/],
  [/\btn\b/, /\b(tn|usmca|nafta)\b/],
  [/\b(stem )?opt\b|\bf-?1\b/, /\bopt\b|\bf-?1\b/],
  [/\bcpt\b/, /\bcpt\b/],
  [/\bl-?1\b/, /\bl-?1\b/],
  [/\be-?3\b/, /\be-?3\b/],
  [/\bo-?1\b/, /\bo-?1\b/],
  [/\bj-?1\b/, /\bj-?1\b/],
  [/\bblue card\b/, /\bblue card\b/],
];

/**
 * "Yes, <visa>" options under a sponsorship question ("No | Yes, EU Blue Card
 * | Yes, USMCA Professional (TN) Visa (USA) | Yes, F-1 Visa OPT (USA) | Yes,
 * but not one of the visas listed here", GitLab, live 2026-10-05): the one
 * side the need picks, then the Yes naming the applicant's own visa, then the
 * Yes saying it is not listed. A need with no visa stated picks none.
 */
function chooseSponsorshipOption(options: string[], need: boolean, stated: string, now: boolean | null): string | null {
  // By what each option says, before its leading word: "No, I do not
  // currently require sponsorship, but I will require sponsorship in the
  // future." is a need that starts later (Trulioo, Ashby bank 2026-10-08:
  // the OPT holder took "Yes, I currently require sponsorship").
  const said = options.filter((o) => o.trim()).map((o) => ({ o, t: qnorm(o) }));
  // "I do not require sponsorship right now, but will at some point in the
  // future" and "I do not and will not require sponsorship at any point in
  // the future" (SEP on Lever, question bank 2026-10-08) too.
  const later = said.filter(
    (x) =>
      (/\b(will|would|may)\b[^,;]{0,30}\b(require|need)\b[^,;]{0,40}\b(future|later)\b/.test(x.t) && /\bnot (currently|now)\b|\bno\b/.test(x.t)) ||
      /\b(not|no)\b[^;]{0,40}\b(now|currently|at this time)\b[^;]{0,20}\bbut\b[^;]{0,20}\b(will|would|may)\b[^;]{0,50}\b(future|later)\b/.test(x.t)
  );
  const never = said.filter(
    (x) =>
      /\b(not|never)\b[^,;]{0,30}\b(require|need)\b[^,;]{0,40}\b(now|currently)\b[^,;]{0,10}\b(or|nor)\b[^,;]{0,15}\b(future|ever|later)\b/.test(x.t) ||
      /\b(do not|dont) and will not (require|need)\b|\bwill (not|never) (ever )?(require|need)\b[^;]{0,50}\b(any point|any time|ever)\b/.test(x.t)
  );
  if (later.length === 1 && never.length === 1) {
    if (!need) return never[0].o;
    if (now === false) return later[0].o;
    // The need now: an option that asks for it, beside "in the process of
    // obtaining permanent work authorization" (SEP).
    const current = said.filter((x) => x !== later[0] && x !== never[0] && /\b(require|need)\b/.test(x.t) && !/\b(not|never)\b/.test(x.t));
    if (now === true && current.length === 1) return current[0].o;
    return null;
  }
  const side = options.filter((o) => o.trim() && optionPolarity(o) === need);
  if (side.length === 1) return side[0];
  if (!need || side.length === 0) return null;
  // "Yes, … now" beside "Yes, … in the future" (Airbnb, question bank
  // 2026-10-05): when the need starts.
  const nowOpt = side.filter((o) => /\b(now|currently|immediately)\b/.test(qnorm(o)) && !/\b(future|later)\b/.test(qnorm(o)));
  const laterOpt = side.filter((o) => /\b(future|later)\b/.test(qnorm(o)) && !/\b(now|currently|immediately)\b/.test(qnorm(o)));
  if (nowOpt.length === 1 && laterOpt.length === 1) return now === null ? null : now ? nowOpt[0] : laterOpt[0];
  const mine = VISA_NAMES.filter(([own]) => own.test(stated.toLowerCase()));
  if (mine.length === 0) return null;
  const named = side.filter((o) => mine.some(([, named]) => named.test(o.toLowerCase())));
  if (named.length > 0) return named.length === 1 ? named[0] : null;
  const unlisted = side.filter((o) => /\bnot (one of|listed|among)\b|\b(an|any )?other\b|\bnone of\b/.test(o.toLowerCase()));
  return unlisted.length === 1 ? unlisted[0] : null;
}

/**
 * A list of visa statuses: "I currently hold an H-1B visa and would need a
 * transfer to Base" | "I currently hold STEM OPT and will require H-1B
 * sponsorship in the future" | … (Base Power), "STEM OPT" | "Non-US Person
 * with work Authorization w/ H1B, H-4" (Vital Lyfe), "H-1B" | "H-1B
 * (Transfer)" | "F-1 Student (STEM OPT)" (OnePay); Ashby bank 2026-10-08, all
 * blank. The option for the visa the applicant holds, by their own
 * statement; a visa named only as a need ("will require H-1B sponsorship",
 * "require initial H-1B") is not held. A citizen of another country takes
 * the option naming that citizenship, where nothing says "not authorized".
 */
function resolveVisaStatusList(q: QuestionInput, facts: ProfileFacts, profile: UserApplicationProfile): QuestionResult {
  const opts = (q.options ?? []).filter((o) => o.trim());
  if (opts.length < 3 || isBooleanQuestion(q)) return null;
  const low = opts.map((o) => o.toLowerCase());
  if (low.filter((t) => VISA_NAMES.some(([, named]) => named.test(t))).length < 2) return null;
  const stated = (profile.workAuthorization || "").toLowerCase();
  const mine = VISA_NAMES.find(([own]) => own.test(stated));
  if (!mine) {
    if (low.some((t) => /\bnot (currently )?(authori[sz]ed|eligible)\b/.test(t))) return null;
    const citizen = [...facts.workAuth.byCountry.entries()].filter(([c, a]) => a.basis === "citizen" && c !== "US").map(([c]) => c);
    if (citizen.length !== 1) return null;
    // "Canadian or Mexican Citizen (TN Visa)" names the applicant; "TN
    // (Applicable for citizens of Canada or Mexico)" names a visa a citizen
    // MAY use (Waymo), which nobody stated.
    const openTo = /\b(applicable|available|open|valid|limited|restricted|only)\s+(for|to)\s+(citizens?|nationals?)\b/i;
    const hits = opts.filter((o) => /\bcitizens?\b/i.test(o) && !openTo.test(o) && countriesNamedIn(o).includes(citizen[0]));
    return hits.length === 1 ? answer(hits[0], "visa-list:citizenship") : null;
  }
  const named = mine[1];
  const held = (t: string): boolean => {
    const m = named.exec(t);
    if (!m) return false;
    const before = t.slice(0, m.index);
    if (/\b(require|requires|requiring|need|needs|initial|new|future)\b[^.]{0,25}$/.test(before)) return false;
    return m.index <= 2 || t.split(/\s+/).length <= 4 || /(?:\b(hold|holding|have|having|on|with|currently)\b|\bw\/)[^.]{0,20}$/.test(before);
  };
  let hits = opts.filter((o) => held(o.toLowerCase()));
  for (const word of [/\bstem\b/, /\btransfer\b/]) {
    if (hits.length < 2) break;
    const mineHas = word.test(stated);
    const narrowed = hits.filter((o) => word.test(o.toLowerCase()) === mineHas);
    if (narrowed.length > 0) hits = narrowed;
  }
  return hits.length === 1 ? answer(hits[0], "visa-list") : null;
}

/**
 * "Current or most recent employer", "Current/Last Company", "Where have you
 * most recently worked?" (Rivian, Plaid, Harvey, Snowflake; Ashby bank
 * 2026-10-08): a job that has ended counts. Only a running one was taken, so
 * a career gap left the company blank beside the title it filled. "Previous
 * employer" is the one before the current: not this.
 */
function resolveRecentEmployer(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (isBooleanQuestion(q) || (q.options?.length ?? 0) > 0) return null;
  const asks =
    /\b(current or (most )?recent|current (or )?last|current or latest|current most recent|most recent|latest|last) (employer|company|organi[sz]ation)\b/.test(n) ||
    /\bwhere (have|did) you (most recently|last) work(ed)?\b/.test(n);
  if (!asks) return null;
  if (isHigh(facts.employment.currentCompany)) return answer(facts.employment.currentCompany.value, "recent-employer:current");
  return isHigh(facts.employment.mostRecentCompany) ? answer(facts.employment.mostRecentCompany.value, "recent-employer") : null;
}

function resolveWorkAuthorization(q: QuestionInput, n: string, facts: ProfileFacts, profile: UserApplicationProfile, ctx: QuestionContext): QuestionResult {
  if (RIGHT_AS_CITIZEN(n)) return null;
  // An agreement that "restricts your right to work for WD" (Western Digital,
  // SmartRecruiters bank, round 7) is a non-compete, not a work right: Yes for
  // everyone authorized. The non-compete default answers it.
  if (NON_COMPETE.test(n)) return null;
  const h1b = resolveH1bHistory(q, n, profile);
  if (h1b) return h1b;
  const opt = resolveOptExtension(q, n, profile);
  if (opt) return opt;
  // A notice to acknowledge, its only option "Yes" or "I acknowledge" ("…By
  // submitting an application, I acknowledge that I have read and understand
  // the E-verify notice", Riot Games; question bank 2026-10-05): no work
  // right is asked. The acknowledgement defaults take it.
  // A "Select..." placeholder is no option (Riot's notice under it was read
  // as a work-right question, question bank 2026-10-05).
  const only = (q.options ?? []).filter((o) => o.trim() && !/^(select|choose|please select|select an option|select one|--)/i.test(o.trim()));
  if (only.length === 1 && /\b(i acknowledge|i have read|i understand|acknowledge that)\b/.test(n)) return null;
  // Accept / Decline answer a statement, not a work right: SSCI's
  // certification ends "…required to verify identity and eligibility to work
  // in the United States…" (Workable bank, 2026-10-05).
  if (only.length >= 2 && only.some(isConsentOption) && only.every((o) => isConsentOption(o) || /^(i )?(decline|disagree|reject|do not (accept|agree))\b/i.test(o.trim()))) return null;
  // The label says what Yes means: "By selecting 'Yes,' you confirm that you
  // do not require Visa Sponsorship" (Peloton, question bank 2026-10-05: every
  // applicant got the inverse). That statement is what gets answered.
  const means = YES_MEANS.exec(n);
  if (means && (SPONSOR.test(means[5]) || WORK_RIGHT.test(means[5])) && (isBooleanQuestion(q) || isYesNoDropdown(q, n))) {
    const said = means[5];
    const where = targetCountry(q, ctx, facts);
    const home = residenceOf(facts);
    const a = authorizedIn(facts.workAuth, where, home);
    const s = needsSponsorshipIn(facts.workAuth, where, home);
    if (SPONSOR.test(said)) {
      if (!isHigh(s)) return abstain("work-auth:yes-means-unknown");
      const ruledOut = NEGATED_NEED.test(said);
      // "…authorized to work … without sponsorship": the right as well.
      if (ruledOut && WORK_RIGHT.test(said) && isHigh(a) && a.value === false) return booleanResult(false, q, "work-auth:yes-means");
      return booleanResult(ruledOut ? !s.value : s.value, q, "work-auth:yes-means");
    }
    if (!isHigh(a)) return abstain("work-auth:yes-means-unknown");
    return booleanResult(a.value, q, "work-auth:yes-means");
  }
  const hasSponsor = SPONSOR.test(n);
  // "Will you require sponsorship … to legally work in the U.S.?" (ZipRecruiter,
  // live 2026-10-03) asks about SPONSORSHIP; its work-right words are only the
  // purpose. A work-right phrase counts when it is asked ("are you authorized…").
  const sponsorAsked = hasSponsor && REQUIRES_SPONSOR.test(n) && !ASKS_RIGHT.test(n) && !STATES_RIGHT.test(n);
  const hasRight = !sponsorAsked && (WORK_RIGHT.test(n) || isAbleToWorkInCountry(n, q.label));
  if (!hasRight && !hasSponsor && !NEEDS_RIGHT.test(n)) return null;
  // "Will you require relocation assistance or visa sponsorship?" asks two
  // things; the sponsorship half alone cannot answer it.
  if (hasSponsor && /\brelocat/.test(n)) return abstain("sponsorship:compound-question");
  // "What is your current visa status?" in a text box (Sigma on Greenhouse,
  // live 2026-10-17) asks for the status, and got "No", the sponsorship
  // answer. The applicant's own statement of it is the answer.
  // ("Will you require sponsorship for employment visa status…? If yes, …"
  // asks the need, not the status: not this.)
  // (A "work authorization status" is a country's, answered below.)
  if (/\b(visa|immigration|citizenship|residency) status\b/.test(n) && !REQUIRES_SPONSOR.test(n) && !isBooleanQuestion(q) && !(q.options ?? []).some((o) => o.trim()) && q.kind !== "number" && q.kind !== "date") {
    const stated = (profile.workAuthorization || "").trim();
    return stated ? answer(stated, "visa-status:text") : abstain("visa-status:unknown");
  }
  // "If you're not authorized to work at the stated location, what sponsorship
  // would you require for the role?" (Brex, live 2026-10-03) wants the TYPE,
  // and got the work-authorization statement ("Canadian citizen").
  const country = targetCountry(q, ctx, facts);
  const residence = residenceOf(facts);
  // The need asked, its details only on Yes: "Would you require any form of
  // Sponsorship or Visa? If Yes, please elaborate below." (Artera on Lever,
  // live, round 7) was left blank for citizens as a question of the visa's
  // type. No for someone who needs none; a Yes is theirs to explain.
  if (hasSponsor && !isBooleanQuestion(q) && !(q.options ?? []).some((o) => o.trim()) && q.kind !== "number" && q.kind !== "date" && /^(would|will|do|does|are|is) you\b/.test(n) && /\bif (yes|so)\b[^?]*\b(elaborate|explain|specify|describe|list|provide|detail)\b/.test(n)) {
    const s = needsSponsorshipIn(facts.workAuth, country, residence);
    if (isHigh(s) && s.value === false && profile.requiresSponsorship?.trim()) return answer("No", "sponsorship:text-no");
    return abstain("sponsorship:type-unknown");
  }
  if (hasSponsor && SPONSOR_TYPE.test(n) && !isBooleanQuestion(q)) return abstain("sponsorship:type-unknown");

  // "Are you prevented from lawfully becoming employed in the US because of
  // visa or immigration status?" (Barnes & Thornburg, Ashby bank 2026-10-08):
  // the inverse of the right, never "do you need sponsorship?" (the OPT
  // holder, authorized today, said Yes). A visa tied to one employer (H-1B)
  // is the applicant's to judge.
  if (/\b(prevented|barred|prohibited|precluded) from (lawfully |legally )?(becoming |being )?(employed|working|employment)\b/.test(n) && (isBooleanQuestion(q) || isYesNoDropdown(q, n))) {
    const a = authorizedIn(facts.workAuth, country, residence);
    if (!isHigh(a)) return abstain("work-auth-prevented:unknown");
    if (a.value === false) return booleanResult(true, q, "work-auth-prevented");
    const basis = country ? facts.workAuth.byCountry.get(country)?.basis : undefined;
    if (basis === "citizen" || basis === "permanent_resident" || basis === "student") return booleanResult(false, q, "work-auth-prevented");
    return abstain("work-auth-prevented:employer-tied");
  }

  // "Permanent work authorization", "without restriction(s)": a citizen's or
  // permanent resident's right. A visa holder's (H-1B, OPT, a work permit) is
  // neither, and was answered Yes (Airtable, Everlaw, MyFundedFutures, Warp;
  // question bank 2026-10-05). Either of two countries named counts.
  if (hasRight && PERMANENT_RIGHT.test(n)) {
    // "In what countries do you have the unrestricted right to work?" (Elastic)
    // asks for the countries: those of a citizen's or permanent resident's.
    if (/\b(what|which) countr(y|ies)\b/.test(n) && !q.options?.length) {
      const lasting = [...facts.workAuth.byCountry.entries()]
        .filter(([, a]) => a.authorized === true && (a.basis === "citizen" || a.basis === "permanent_resident"))
        .map(([cc]) => countryByCode(cc)?.name)
        .filter((x): x is string => Boolean(x));
      return lasting.length > 0 ? answer(lasting.join(", "), "work-auth-permanent:countries") : abstain("work-auth-permanent:countries-unknown");
    }
    const named = countriesNamedIn(q.label);
    const where = named.length > 0 ? named : country ? [country] : [];
    if (where.length === 0) return abstain("work-auth-permanent:no-country");
    const auths = where.map((c) => facts.workAuth.byCountry.get(c));
    const lasting = (a: (typeof auths)[number]): boolean => a?.authorized === true && (a.basis === "citizen" || a.basis === "permanent_resident");
    const settled = auths.every((a) => a && (a.authorized === false || ["citizen", "permanent_resident", "work_permit", "student"].includes(a.basis)));
    const permanent = auths.some(lasting);
    if (!permanent && !settled) return abstain("work-auth-permanent:unknown");
    if (isBooleanQuestion(q) || isYesNoDropdown(q, n)) return booleanResult(permanent, q, "work-auth-permanent");
    if ((q.kind === "text" || q.kind === "longText") && !q.options?.length) return answer(permanent ? "Yes" : "No", "work-auth-permanent:text");
  }

  // "Are you eligible to work in Canada without sponsorship?" = authorized AND no sponsorship.
  // "I am authorized … and will not require visa sponsorship" affirms the same.
  if (hasRight && (WITHOUT_SPONSOR.test(n) || (STATES_RIGHT.test(n) && hasSponsor && NEGATED_NEED.test(n)))) {
    if (!isBooleanQuestion(q)) return abstain("work-auth-without-sponsorship:not-boolean");
    const a = authorizedIn(facts.workAuth, country, residence);
    const s = needsSponsorshipIn(facts.workAuth, country, residence);
    if (isHigh(a) && a.value === false) return booleanResult(false, q, "work-auth-without-sponsorship:not-authorized");
    if (isHigh(s) && s.value === true) {
      // "CURRENTLY able to work … without sponsorship?" (OnLogic): OPT needs
      // none until it ends; an H-1B is sponsored now.
      // The ASKED sentence's "currently": "This position is not currently
      // available for H-1B visa sponsorship. Are you authorized … without need
      // for sponsorship?" (RentVision) asks about any time.
      if (NOW_ONLY(qnorm(askedSentenceOf(q.label))) && isHigh(a) && a.value === true && !sponsorshipNeededNow(n, facts, profile, country, residence)) {
        return booleanResult(true, q, "work-auth-without-sponsorship:not-now");
      }
      return booleanResult(false, q, "work-auth-without-sponsorship:needs-sponsorship");
    }
    if (isHigh(a) && a.value === true && isHigh(s) && s.value === false) {
      return booleanResult(true, q, "work-auth-without-sponsorship");
    }
    return abstain("work-auth-without-sponsorship:unknown");
  }

  if (hasSponsor && !hasRight) {
    // "Will you now or in the future require sponsorship…"
    const s = needsSponsorshipIn(facts.workAuth, country, residence);
    const now = isHigh(s) && s.value === true ? sponsorshipNeededNow(n, facts, profile, country, residence) : null;
    if (!isBooleanQuestion(q)) {
      // "…please list the type of support you may require" (Pinterest,
      // question bank 2026-10-05) asks for a description: never a yes or no.
      if (/\b(list|describe|specify|explain|provide|detail|elaborate)\b|\b(type|kind|form) of (support|assistance|sponsorship|visa|status)\b/.test(n)) {
        return abstain("sponsorship:type-unknown");
      }
      if (q.options?.some((o) => o.trim())) {
        const pick = isHigh(s) ? chooseSponsorshipOption(q.options, s.value, profile.workAuthorization || "", now) : null;
        return pick ? answer(pick, "sponsorship:option") : abstain("sponsorship:option-unknown");
      }
      // Free text: the applicant's own stated answer, verbatim, when it covers this country.
      if (isHigh(s) && profile.requiresSponsorship?.trim() && q.kind !== "number" && q.kind !== "date") {
        return answer(s.value ? "Yes" : "No", "sponsorship:text");
      }
      return abstain("sponsorship:unknown");
    }
    // "Will you NOW require sponsorship…?" beside "…in the FUTURE…?" (DoorDash,
    // live 2026-10-05: both Yes for an OPT holder). "Do you now, or will you
    // ever, require…" (Toast, question bank 2026-10-05) asks about later too.
    const nowOnly = NOW_ONLY(n);
    if (nowOnly && now === false) return booleanResult(false, q, "sponsorship:not-now");
    // A question phrased as the inverse ("Can you work WITHOUT sponsorship?")
    // without a work-right phrase ("…work for us without sponsorship").
    const inverted = /\bwithout\b/.test(n) && !/\b(require|need)\b/.test(n);
    if (isHigh(s)) return booleanResult(inverted ? !s.value : s.value, q, "sponsorship");
    return abstain("sponsorship:unknown");
  }

  // "Do you require work authorization?" (Waymo on Greenhouse, live
  // 2026-10-05) asks whether the right is NEEDED, the inverse of having it:
  // read as "are you authorized?", a Canadian on a US job answered No (and a
  // US citizen would have answered Yes).
  if (!hasSponsor && NEEDS_RIGHT.test(n) && !ASKS_RIGHT.test(n)) {
    // "Will you now or in the future require authorization to work in the
    // United States?" (TensorWave, Ashby bank 2026-10-08; "now or in the
    // future" had put it out of reach and it was read as "are you
    // authorized?", backwards): a right that ends (OPT) or is tied to an
    // employer (H-1B) is needed again, which is the sponsorship need.
    if (/\b(future|ever|later)\b/.test(n) && (isBooleanQuestion(q) || isYesNoDropdown(q, n))) {
      const s = needsSponsorshipIn(facts.workAuth, country, residence);
      if (isHigh(s)) return booleanResult(s.value, q, "work-auth-needed:future");
    }
    const a = authorizedIn(facts.workAuth, country, residence);
    if (!isHigh(a)) return abstain("work-auth-needed:unknown");
    if (isBooleanQuestion(q) || isYesNoDropdown(q, n)) return booleanResult(!a.value, q, "work-auth-needed");
    if ((q.kind === "text" || q.kind === "longText") && !q.options?.length) return answer(a.value ? "No" : "Yes", "work-auth-needed:text");
    return abstain("work-auth-needed:not-yes-no");
  }

  // Work authorization proper. A dropdown whose options load only when opened
  // ("Are you legally authorized to work in the country that you are located?",
  // a Netlify react-select) is a yes/no by its words.
  if (isBooleanQuestion(q) || isYesNoDropdown(q, n)) {
    // "…in the U.S. or Canada?": the right in either one.
    const named = countriesNamedIn(q.label);
    if (named.length >= 2 && /\bor\b/.test(n)) {
      const each = named.map((c) => authorizedIn(facts.workAuth, c, residence));
      if (each.some((a) => isHigh(a) && a.value === true)) return booleanResult(true, q, "work-auth:either");
      if (each.every((a) => isHigh(a) && a.value === false)) return booleanResult(false, q, "work-auth:either");
      return abstain("work-auth:unknown");
    }
    const a = authorizedIn(facts.workAuth, country, residence);
    if (isHigh(a)) return booleanResult(a.value, q, "work-auth");
    return abstain("work-auth:unknown");
  }
  // A status choice ("What is your work authorization status?"), or free text.
  if (q.options && q.options.length > 0) {
    return (
      resolveCountryEligibility(q, facts, residence) ??
      resolveStatusChoice(q, facts, country) ??
      resolveAuthorizationStatement(q, facts, country, residence) ??
      abstain("work-auth-status:unknown")
    );
  }
  if (q.kind === "text" || q.kind === "longText") {
    const stated = profile.workAuthorization?.trim();
    // Only when the statement covers the country asked about (or none is named).
    if (stated && (!country || facts.workAuth.byCountry.has(country) || facts.workAuth.byCountry.size === 0)) {
      return answer(stated, "work-auth:statement");
    }
  }
  return abstain("work-auth:unknown");
}

/**
 * Options that are STATEMENTS about the right to work: "I am authorized to work
 * in the United States for any employer", "…for my present employer only", "I
 * require sponsorship…", "I am not authorized…" (SpaceX on Greenhouse, live
 * 2026-10-03: two options read as a yes, so a green-card holder went to the
 * AI). The one the profile's authorization and sponsorship make true.
 */
function resolveAuthorizationStatement(q: QuestionInput, facts: ProfileFacts, country: string | null, residence: string | null): QuestionResult {
  const opts = (q.options ?? []).map((o) => ({ raw: o, n: qnorm(o), pol: optionPolarity(o) }));
  type Opt = (typeof opts)[number];
  const one = (keep: (o: Opt) => boolean): string | null => {
    const hits = opts.filter(keep);
    return hits.length === 1 ? hits[0].raw : null;
  };
  // "I require sponsorship…", "Yes, but I will need sponsorship in the future",
  // "No, I need sponsorship now"; never "I do not require sponsorship".
  const requires = (o: Opt) => /\b(require|requires|need|needs)\b/.test(o.n) && /\bsponsor/.test(o.n) && !NEGATED_NEED.test(o.n);
  const notAuthorized = (o: Opt) => /\b(am|is|are) not (legally |currently )?(authorized|eligible|permitted)\b/.test(o.n);
  const a = authorizedIn(facts.workAuth, country, residence);
  const s = needsSponsorshipIn(facts.workAuth, country, residence);
  // An option's own Yes or No answers the work right: "Yes, but I will need
  // sponsorship in the future" claims it (Datadog, question bank 2026-10-05:
  // written for an applicant with no US work right at all). Only a leading
  // Yes or No: "I require … sponsorship" reads affirmative, and says the
  // opposite of authorized (Lyft).
  const yn = (o: Opt): boolean | null => (/^(yes|no)\b/.test(o.n) ? o.pol : null);
  const fitsRight = (o: Opt) => !isHigh(a) || yn(o) === null || yn(o) === a.value;
  // A need the applicant STATED decides, whatever the work right (a student
  // visa's is unclear); one only inferred from "not authorized" does not, and
  // the literal answer is then "not authorized".
  if (facts.workAuth.statedSponsorship === true && isHigh(s) && s.value === true) {
    const pick = one((o) => requires(o) && fitsRight(o));
    if (pick) return answer(pick, "work-auth-statement:needs-sponsorship");
    if (!isHigh(a) || a.value !== false) return null;
  }
  if (!isHigh(a)) return null;
  if (a.value === false) {
    const pick =
      one(notAuthorized) ??
      one((o) => yn(o) !== true && requires(o)) ??
      // A bare "No" beside statements of the right ("Yes, I am a Canadian
      // citizen…", "Yes, I have a valid permit", Coveo).
      one((o) => o.pol === false && !requires(o));
    return pick ? answer(pick, "work-auth-statement:not-authorized") : null;
  }
  if (!isHigh(s)) return null;
  if (s.value === true) {
    const pick = one((o) => yn(o) !== false && requires(o));
    return pick ? answer(pick, "work-auth-statement:needs-sponsorship") : null;
  }
  const pick =
    one((o) => /\b(authorized|eligible|permitted)\b/.test(o.n) && /\bany employer\b/.test(o.n) && !/\bnot\b/.test(o.n)) ??
    // "Yes, no restriction." beside "Yes, but I will need sponsorship…" (Datadog).
    one((o) => o.pol === true && !/\bsponsor/.test(o.n) && !/\b(present|current) employer only\b/.test(o.n));
  return pick ? answer(pick, "work-auth-statement:any-employer") : null;
}

/**
 * "Are you legally eligible to work in Canada or the USA?" [Yes - … in Canada |
 * Yes - … in the USA | No - … in Canada or the USA] (Trulioo, Ashby bank
 * 2026-10-08): each Yes names its country. The one where the applicant may
 * work; the No when they may work in none of them. Read for the job's country
 * alone, a Canadian citizen said No.
 */
function resolveCountryEligibility(q: QuestionInput, facts: ProfileFacts, residence: string | null): QuestionResult {
  // Only the shape that asks it: the question names two or more countries
  // with "or", and each Yes names a different one of them (SpaceX's "…for any
  // employer" / "…for my present employer only" both name the US).
  const asked = countriesNamedIn(q.label);
  if (asked.length < 2 || !/\bor\b/.test(qnorm(q.label))) return null;
  const opts = (q.options ?? []).filter((o) => o.trim()).map((o) => ({ raw: o, pol: optionPolarity(o), places: countriesNamedIn(o) }));
  const yes = opts.filter((o) => o.pol === true && o.places.length === 1 && asked.includes(o.places[0]));
  if (yes.length < 2 || new Set(yes.map((o) => o.places[0])).size !== yes.length) return null;
  const may = yes.map((o) => ({ o, a: authorizedIn(facts.workAuth, o.places[0], residence) }));
  const can = may.filter((x) => isHigh(x.a) && x.a.value === true);
  if (can.length === 1) return answer(can[0].o.raw, "work-auth:country-option");
  if (can.length > 1) return abstain("work-auth:country-option-several");
  const no = opts.filter((o) => o.pol === false);
  if (no.length === 1 && may.every((x) => isHigh(x.a) && x.a.value === false)) return answer(no[0].raw, "work-auth:country-option-none");
  return abstain("work-auth:country-option-unknown");
}

/** A citizenship / status option for a status choice question. */
function resolveStatusChoice(q: QuestionInput, facts: ProfileFacts, country: string | null): QuestionResult {
  const opts = q.options ?? [];
  const entries = [...facts.workAuth.byCountry.entries()].filter(([code]) => !country || code === country);
  if (entries.length !== 1) return null;
  const [code, auth] = entries[0];
  const countryName = countryByCode(code)?.name ?? "";
  const want =
    auth.basis === "citizen"
      ? /\bcitizen/
      : auth.basis === "permanent_resident"
        ? /\b(permanent resident|green card|lawful permanent)\b/
        : null;
  if (!want) return null;
  const hits = opts.filter((o) => want.test(qnorm(o)));
  // Prefer the one naming the right country when several mention citizenship.
  const named = hits.filter((o) => countryNamedIn(o) && (countryNamedIn(o) as { code: string }).code === code);
  const pick = named.length === 1 ? named[0] : hits.length === 1 && !countryNamedIn(hits[0]) ? hits[0] : null;
  return pick ? answer(pick, `work-auth-status:${auth.basis}:${countryName}`) : null;
}

// "Check every country of which you are a citizen" (Open Data Jobs on
// Workable, 2026-10-05) got the country of RESIDENCE: US for an H-1B worker.
// "…authorized to work in the U.S. as a U.S. citizen…" (Credence) asks
// citizenship, not any work right.
// A nationality is a citizenship: "Please indicate your nationality:" over
// "Afghan | … | British | …" (Canonical on Greenhouse, live, round 8) went to
// the AI for a stated British citizen.
const CITIZEN_Q = /\b(are you|is the applicant) (a |an )?((u ?s|us|united states|canadian|american|british|uk) )?citizen\b|\bcitizen of\b|\bcitizenship\b|\bwhich you are an? citizen\b|\bas an? ((u ?s|us|united states|canadian|american|british|uk) )?citizen\b|\b(your|current|applicant s|candidate s) nationalit(y|ies)\b|^nationalit(y|ies)\b/;
/** A work right asked only as a citizen's (no visa or other status beside it). */
const RIGHT_AS_CITIZEN = (n: string): boolean =>
  /\bas an? ((u ?s|us|united states|canadian|american|british|uk) )?citizen\b/.test(n) && !/\b(visa|permit|permanent resident|green card|other|status)\b/.test(n);

/**
 * "Since obtaining your most recent citizenship, did you afterwards become a
 * permanent resident in any other country/region?" (Amazon's export questions,
 * on Twitch's Greenhouse form, live 2026-10-03: answered "United States").
 * A stated permanent residency is one taken in a country not theirs: Yes. A
 * citizen living in a country of their citizenship, with no residency stated
 * anywhere: No. Temporary statuses do not count, by the question's own terms.
 */
function resolveLaterResidency(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\b(since|after) (obtaining|acquiring|receiving|gaining|becoming)\b.*\bcitizen(ship)?\b.*\bpermanent resident\b/.test(n)) return null;
  if (!isBooleanQuestion(q) && q.options?.length) return null;
  const known = [...facts.workAuth.byCountry.entries()];
  if (known.some(([, a]) => a.basis === "permanent_resident")) return booleanResult(true, q, "later-residency:pr");
  const citizenOf = new Set(known.filter(([, a]) => a.basis === "citizen").map(([cc]) => cc));
  const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
  if (home && citizenOf.has(home)) return booleanResult(false, q, "later-residency:none");
  return abstain("later-residency:unknown");
}

function resolveCitizenship(q: QuestionInput, n: string, facts: ProfileFacts, ctx: QuestionContext): QuestionResult {
  if (!CITIZEN_Q.test(n)) return null;
  // Consent to be ASKED about citizenship is consent ("Do you consent to
  // Lodestar Space requesting information regarding citizenship, passports
  // and visas…?", question bank 3): it was answered as citizenship itself.
  if (/\b(do you|you) (consent|agree|authori[sz]e)\b/.test(n)) return null;
  // Citizenship of a sanctioned country is never US citizenship, though the
  // label names "U.S. export control laws": a US citizen said Yes to being a
  // citizen of Cuba, Iran, North Korea or Syria (Asana, Intercom; question
  // bank 2026-10-05).
  if (SANCTIONED.test(n)) return null;
  const country = countryNamedIn(q.label);
  const code = country === "this-country" ? ctx.jobCountry : country?.code ?? null;
  const known = facts.workAuth.byCountry;
  // A yes/no in words ("…requires US citizenship…, do you meet that
  // requirement?", Striveworks on Greenhouse, live 2026-10-03) is a yes/no
  // even before its options load: it was answered "Canada".
  const yesNoWords = /\b(do|are|can|will|would) you\b[^?]*\??\s*$|\bdo you meet\b/.test(n) && !/\b(which|what) (country|countries)\b/.test(n);
  // A dual citizen holds two: "Are you a dual citizen (including U.S.
  // citizenship) or a legal permanent resident of the United States?"
  // (LLNL, SmartRecruiters bank, round 7) was Yes for a US citizen alone.
  if (/\b(dual|multiple) (citizen|citizenship|national|nationality)\b|\bmore than one (citizenship|nationality)\b/.test(n) && isBooleanQuestion(q)) {
    const held = [...known.entries()].filter(([, a]) => a.basis === "citizen").map(([c]) => c);
    if (held.length >= 2) return booleanResult(true, q, "citizenship:dual");
    if (/\bpermanent resident|green card\b/.test(n) && code && known.get(code)?.basis === "permanent_resident") return booleanResult(true, q, "citizenship:dual-or-pr");
    // "…(including U.S. citizenship) or a legal permanent resident of the
    // United States?" for someone not allowed to work there, or there on a
    // visa: neither (LLNL; bank r7d left BERLIN_STAFF's No blank).
    if (code && known.get(code)?.authorized === false) return booleanResult(false, q, "citizenship:not-authorized");
    if (code && (known.get(code)?.basis === "work_permit" || known.get(code)?.basis === "student")) return booleanResult(false, q, "citizenship:visa");
    return held.length === 1 ? booleanResult(false, q, "citizenship:one") : abstain("citizenship:unknown");
  }
  if (isBooleanQuestion(q) || (yesNoWords && !(q.options && q.options.length))) {
    // Not allowed to work there: certainly not a citizen there.
    if (code && known.get(code)?.authorized === false) return booleanResult(false, q, "citizenship:not-authorized");
    if (!code) return abstain("citizenship:no-country");
    const c = known.get(code);
    if (!c) return abstain("citizenship:unknown");
    const orPr = /\bpermanent resident|green card\b/.test(n);
    if (c.basis === "citizen") return booleanResult(true, q, "citizenship");
    if (c.basis === "permanent_resident") return booleanResult(orPr, q, "citizenship:pr");
    if (c.basis === "work_permit" || c.basis === "student") return booleanResult(false, q, "citizenship:visa");
    return abstain("citizenship:unknown");
  }
  // "Citizenship Status" asks a STATUS ("(b) U.S. lawful permanent resident"),
  // not a country: answered from its options once they load, never settled
  // before. Read as a country, a green-card holder's was blocked for good
  // (SpaceX on Greenhouse, live 2026-10-03).
  if (/\bcitizenship status\b|\bstatus of (your )?citizenship\b/.test(n)) {
    if (!q.options?.length) return null;
    return resolveStatusChoice(q, facts, code) ?? abstain("citizenship:status-unknown");
  }
  // "Country of citizenship" choice/text.
  const citizenOf = [...known.entries()].filter(([, a]) => a.basis === "citizen").map(([cc]) => countryByCode(cc)!);
  if (citizenOf.length !== 1) return abstain("citizenship:unknown");
  return renderCountry(citizenOf[0], q, "citizenship:country");
}

// ----- Residence ------------------------------------------------------------

// "…or are currently in a 24-month OPT extension based upon a degree from a
// qualifying U.S. institution" is no residence (Duolingo, live 2026-10-05:
// answered Yes as "are you in the US?"): never "in a/an" something.
// French too: "Résidez-vous actuellement au Canada?" (Mila on Workable, 2026-10-05).
// "Are you local to the Germantown, MD office (within 25 miles)" (Data Lab on
// Lever, question bank 2026-10-08) is where one lives, not a requirement.
const RESIDE = /\b(live|living|reside|residing|resident|located|based|currently in(?! an? )|located within|within commuting distance|local to|residez|resider|habitez|habiter)\b/;

const METRO_ALIASES: Record<string, string> = { nyc: "new york", gta: "toronto", "bay area": "san francisco", sf: "san francisco" };

/**
 * The place a residence question names, found by SCANNING the label for a
 * place we know (country, state/province full name, major city or metro),
 * rather than by parsing its grammar: real labels interleave the place with
 * clauses ("based in or planning to relocate to the NYC area and able to…").
 */
function placeIn(label: string): { kind: "country"; code: string } | { kind: "region"; code: string; country: string } | { kind: "city"; name: string; region?: { code: string; country: string } } | null {
  const country = countryNamedIn(label);
  if (country && country !== "this-country") return { kind: "country", code: country.code };
  const n = ` ${geoNorm(label)} `;
  // Region FULL names only: two-letter codes collide with words ("in", "or", "me").
  for (const r of [...US_STATES_LIST, ...CA_PROVINCES_LIST]) {
    for (const name of [r.name, ...(r.aliases ?? [])]) {
      const key = geoNorm(name);
      // "New York" is a state AND a city; "New York City"/"NYC" is the city.
      if (key === "new york" && /\bnew york city\b|\bnyc\b/.test(n)) continue;
      if (key === "washington" && /\bwashington d ?c\b/.test(n)) continue;
      if (n.includes(` ${key} `)) return { kind: "region", code: r.code, country: r.country };
    }
  }
  for (const [alias, city] of Object.entries(METRO_ALIASES)) {
    if (n.includes(` ${alias} `)) return { kind: "city", name: city };
  }
  for (const city of KNOWN_CITIES) {
    if (n.includes(` ${city} `)) return { kind: "city", name: city };
  }
  return cityWithRegion(label);
}

/** Every US state and Canadian province a label names in full, in order. */
function regionsNamedIn(label: string): Array<{ code: string; country: string; key: string }> {
  const n = ` ${geoNorm(label)} `;
  const out: Array<{ code: string; country: string; key: string }> = [];
  for (const r of [...US_STATES_LIST, ...CA_PROVINCES_LIST]) {
    for (const name of [r.name, ...(r.aliases ?? [])]) {
      const key = geoNorm(name);
      if (key === "new york" && /\bnew york city\b|\bnyc\b/.test(n)) continue;
      if (key === "washington" && /\bwashington d ?c\b/.test(n)) continue;
      if (n.includes(` ${key} `) && !out.some((o) => o.code === r.code && o.country === r.country)) out.push({ code: r.code, country: r.country, key });
    }
  }
  return out;
}

/** A city this file does not know, written with its state or province code:
 *  "the Germantown, MD office" (Data Lab on Lever, question bank 2026-10-08).
 *  Only a real US or Canadian code after the comma. */
function cityWithRegion(label: string): { kind: "city"; name: string; region: { code: string; country: string } } | null {
  for (const m of label.matchAll(/\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),\s*([A-Z]{2})\b/g)) {
    const region = regionFromText(m[2], "US") ?? regionFromText(m[2], "CA");
    if (region && region.code === m[2]) return { kind: "city", name: m[1], region: { code: region.code, country: region.country } };
  }
  return null;
}

/** The APPLICANT is the one who lives/is located somewhere. "This position
 *  requires you to work from the Toronto office located at …" is about the
 *  office; reading it as residence answered a commute question (Lever, live
 *  2026-10-03), and so did "Are you able to commute … the New York HQ office
 *  (located at …)" (Peloton, question bank 2026-10-05). */
const APPLICANT_RESIDES =
  /\b(do|are|have|did) you\b(?:(?!\b(?:office|offices|headquarters|hq|campus|building|facility)\b)[^?]){0,60}?\b(live|living|reside|residing|located|based|resident|currently in(?! an? ))\b|\bare you (a |an )?(current )?resident\b|\bare you (currently )?local to\b|\byour (current )?(location|residence|city of residence|place of residence)\b|\bwhere (do|are) you\b|\b(residez|habitez) vous\b/;

function resolveResidence(q: QuestionInput, n: string, facts: ProfileFacts, profile: UserApplicationProfile): QuestionResult {
  // "By selecting 'Yes,' you confirm that you currently reside in the New
  // York, NY area or are prepared to commute or relocate at your own expense"
  // (Peloton, question bank 2026-10-05): the statement is the question.
  const means = YES_MEANS.exec(n);
  if (means && RESIDE.test(means[5]) && isBooleanQuestion(q)) {
    const said = /\b(?:confirm|certify|acknowledge|attest|agree|declare|state|represent|affirm)s?\s+(?:that\s+)?(?:you|I)\s+(.+)$/i.exec(q.label);
    if (said) {
      const label = `Do you ${said[1].trim()}`;
      return resolveResidence({ ...q, label }, qnorm(label), facts, profile);
    }
  }
  if (!RESIDE.test(n) || !APPLICANT_RESIDES.test(n) || !isBooleanQuestion(q)) return null;
  if (/\b(willing|open|able|plan|planning) to (relocate|move)\b/.test(n) && !/\b(live|reside|located|based)\b/.test(n)) return null;
  // A time zone named ("…the San Francisco Bay Area or within the Pacific time
  // zone", Amplitude; "…based in the Pacific timezone?", Superhuman; Ashby
  // bank 2026-10-08): being in it is a Yes; a No also needs the place ruled out.
  const zone = zoneNamedIn(n);
  const inZone = zone ? applicantInZone(zone, facts) : null;
  if (inZone === true) return booleanResult(true, q, "residence:time-zone");
  const place = placeIn(q.label);
  if (!place) {
    if (!zone) return null;
    return inZone === false ? booleanResult(false, q, "residence:time-zone") : abstain("residence:time-zone-unknown");
  }
  if (zone && inZone === null) return abstain("residence:time-zone-unknown");
  const loc = facts.location;
  const residenceCountry = isHigh(loc.country) ? loc.country.value : null;
  // "…or willing to relocate?", and the other way round: "Are you open to
  // relocating if you're not currently based there?" (Gemini, live 2026-10-05).
  // "…currently based, or planning to be based in NYC…" (Teleskope, Ashby
  // bank 2026-10-08): a planned move is the same clause.
  // Being ABLE to be somewhere is moving there for someone who would: "Are
  // you able to be located in Jacksonville, FL in summer 2026?" (RF Smart,
  // Greenhouse bank 2) was No for everyone willing to move.
  const relocateClause =
    /\b(or|if not)\b[^?]*\b(relocat|move)/.test(n) ||
    /\b(relocat\w*|move)\b[^?]*\bif (you re |you are |youre )?not\b/.test(n) ||
    /\bor (planning|plan|intending|intend|expecting|expect) to (be )?(based|located|living|live|reside|residing)\b/.test(n) ||
    /\b(able|willing|prepared|open) to be (located|based)\b/.test(n);
  const relocate = polarityOf(profile.willingToRelocate || "");

  const yesOrRelocate = (lives: boolean | null, rule: string): QuestionResult => {
    if (lives === null) return abstain(`${rule}:unknown`);
    if (lives) return booleanResult(true, q, rule);
    if (relocateClause) {
      if (relocate === null) return abstain(`${rule}:relocation-unknown`);
      return booleanResult(relocate, q, `${rule}+relocation`);
    }
    return booleanResult(false, q, rule);
  };

  // A list of states and no city: "Kobie operates in the following states.
  // Are you currently located in one of these states? Colorado, Connecticut,
  // District of Columbia, Florida, Georgia, …" (Lever, question bank
  // 2026-10-08) read "Georgia" as the country, and a Colorado resident said No.
  const states = regionsNamedIn(q.label);
  // ("New York" in such a list is the state, not the city.)
  if (states.length >= 2 && !placesIn(q.label).some((p) => p.kind === "city" && !states.some((s) => s.key === geoNorm(p.name)))) {
    if (isHigh(loc.region)) {
      const r = loc.region.value;
      return yesOrRelocate(states.some((s) => s.code === r.code && s.country === r.country), "residence-states");
    }
    if (residenceCountry && !states.some((s) => s.country === residenceCountry.code)) return yesOrRelocate(false, "residence-states:other-country");
    return abstain("residence-states:unknown");
  }
  if (place.kind === "country") {
    if (!residenceCountry) return abstain("residence-country:unknown");
    return yesOrRelocate(residenceCountry.code === place.code, "residence-country");
  }
  if (place.kind === "region") {
    if (isHigh(loc.region)) {
      return yesOrRelocate(loc.region.value.code === place.code && loc.region.value.country === place.country, "residence-region");
    }
    if (residenceCountry && residenceCountry.code !== place.country) return yesOrRelocate(false, "residence-region:other-country");
    return abstain("residence-region:unknown");
  }
  // City / metro ("near Seattle", "the NYC area"): the same city is a Yes; a
  // city in ANOTHER country, or in a state that does not border the
  // applicant's, is a No; anything closer is "near" or not by a judgment we
  // do not make (livesAt). Every city named counts ("the San Francisco Bay
  // Area or New York City", Mixpanel): one is a Yes, a No needs them all.
  const area = AREA_WORDS.test(n);
  const named = placesIn(q.label).filter((p) => p.kind === "city");
  const verdicts = (named.length > 0 ? named : [place]).map((p) => livesAt(p, facts, area));
  if (verdicts.some((v) => v.lives === true)) return yesOrRelocate(true, "residence-city");
  if (verdicts.every((v) => v.lives === false)) return yesOrRelocate(false, `residence-${verdicts[0].why}`);
  // "…based near our New York City, NY office. Are you open to relocating if
  // you're not currently based there?" (Gemini, live 2026-10-05): someone who
  // will move says Yes either way, and the label's own state settles "near"
  // for anyone in another one.
  if (relocateClause && relocate === true) return booleanResult(true, q, "residence-city+relocation");
  const labelled = /\b([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3}),\s*([A-Z]{2})\b/.exec(q.label);
  const region = labelled ? regionFromText(labelled[2]) : null;
  if (region && isHigh(loc.region) && (loc.region.value.code !== region.code || loc.region.value.country !== region.country)) {
    return yesOrRelocate(false, "residence-city:other-region");
  }
  return abstain("residence-city:unknown");
}

type NamedPlace = NonNullable<ReturnType<typeof placeIn>>;

/**
 * Whether the applicant lives at a place a question names. The same city,
 * state or country: true. Another country, or a state that does not border
 * the applicant's: false. Anything closer is a judgment ("near") we do not
 * make: null. A neighbouring state can be the same metro (Jersey City for
 * New York, Gatineau for Ottawa).
 */
function livesAt(place: NamedPlace, facts: ProfileFacts, area = false): { lives: boolean | null; why: string } {
  const loc = facts.location;
  const country = isHigh(loc.country) ? loc.country.value : null;
  const region = isHigh(loc.region) ? loc.region.value : null;
  if (place.kind === "country") return { lives: country ? country.code === place.code : null, why: "country" };
  if (place.kind === "region") {
    if (region) return { lives: region.code === place.code && region.country === place.country, why: "region" };
    return { lives: country && country.code !== place.country ? false : null, why: "region:other-country" };
  }
  if (isHigh(loc.city) && geoNorm(loc.city.value) === geoNorm(place.name)) return { lives: true, why: "city" };
  const placeCountry = countryHintForCity(place.name) ?? place.region?.country ?? null;
  if (placeCountry && country && placeCountry !== country.code) return { lives: false, why: "city:other-country" };
  const placeRegion = regionHintForCity(place.name) ?? place.region?.code ?? null;
  if (placeRegion && region && placeRegion !== region.code && !regionsBorder(placeRegion, region.code)) return { lives: false, why: "city:other-region" };
  // Two cities this file knows: another metro is elsewhere (Toronto for a
  // Montreal office), the same metro is that area ("…the Bay Area", San Jose).
  const metro = isHigh(loc.city) ? sameMetro(loc.city.value, place.name) : null;
  if (metro === false) return { lives: false, why: "city:other-city" };
  if (metro === true && area) return { lives: true, why: "city:same-metro" };
  return { lives: null, why: "city:unknown" };
}

/** Words that make a city its area ("the NYC area", "near Seattle", "commutable distance"). */
const AREA_WORDS = /\b(area|region|metro|greater|near|nearby|vicinity|commut\w*|proximity|within)\b/;

/** Every place a label names, first mention first (see placeIn). */
function placesIn(label: string): NamedPlace[] {
  const out: NamedPlace[] = [];
  const seen = new Set<string>();
  const add = (p: NamedPlace): void => {
    const key = JSON.stringify(p);
    if (!seen.has(key)) {
      seen.add(key);
      out.push(p);
    }
  };
  const first = placeIn(label);
  if (first) add(first);
  const n = ` ${geoNorm(label)} `;
  for (const [alias, city] of Object.entries(METRO_ALIASES)) if (n.includes(` ${alias} `)) add({ kind: "city", name: city });
  for (const city of KNOWN_CITIES) if (n.includes(` ${city} `)) add({ kind: "city", name: city });
  return out;
}

/** A North American time zone a question names ("the Pacific time zone"). */
function zoneNamedIn(n: string): string | null {
  return /\b(pacific|mountain|central|eastern|atlantic|newfoundland) (standard )?(time ?zone|timezone|time)\b/.exec(n)?.[1] ?? null;
}

/** Whether the applicant lives in a North American time zone: by state or
 *  province; anyone in another country is in none of them. */
function applicantInZone(zone: string, facts: ProfileFacts): boolean | null {
  const region = isHigh(facts.location.region) ? facts.location.region.value : null;
  if (region) {
    const theirs = ZONE_OF_REGION[region.country + "-" + region.code];
    return theirs ? theirs === zone : null;
  }
  const country = isHigh(facts.location.country) ? facts.location.country.value.code : null;
  return country && country !== "US" && country !== "CA" && country !== "MX" ? false : null;
}

/** An option that says the applicant must move: "Planning to Relocate", "Yes
 *  but I would need to relocate", "…but I am willing to relocate". */
const MOVE_OPTION = /\b(willing|open|planning|plan|ready|happy|able|prepared) to (relocate|move)\b|\bwould (need|have) to (relocate|move)\b|\b(require|requires|need|needs) (relocation|to relocate)\b/;
const NO_MOVE = /\b(not|never|unable|unwilling)( be)?( (willing|able|open|planning|prepared|ready))? to (relocate|move)\b|\bunwilling\b/;
/** An option that says the applicant already lives there: "Yes, I live in
 *  San Diego", "In Boston", "…I am local to the San Francisco Bay Area". */
const LOCAL_OPTION = /\b(i|we) (currently |already )?(live|reside) (in|near|within)\b|\bi am (currently |already )?(local|located|based|living) (in|to|near|within)\b|\blocal (to|in)\b|^local\b|^in [a-z]|\balready (live|living|located|based)\b/;
/** Who pays for the move is the applicant's to choose ("Ready to move
 *  (Self-Funded)" beside "Relocation Assistance Required", Niantic). */
const MOVE_TERMS = /\b(assistance|package|support|reimburs\w*|stipend|self ?funded|expense)\b/;

/**
 * A list that tells living at the office from moving there from neither:
 * "Yes, I live in San Diego" | "I do not live in San Diego but I am willing to
 * relocate" | "No, …" (Iambic), "In Boston" | "Planning to Relocate" (Pryzm),
 * "Yes" | "No" | "Yes, but require relocation" (Vital Lyfe). Anyone willing
 * to move got the first Yes: people in Toronto, Berlin and Bengaluru said
 * they lived in San Diego (Ashby question bank, 2026-10-08). The place is the
 * one the local option or the question names, else the job's city.
 */
function resolveLocalOrRelocate(q: QuestionInput, facts: ProfileFacts, profile: UserApplicationProfile, ctx: QuestionContext): QuestionResult {
  const opts = (q.options ?? []).filter((o) => o.trim()).map((o) => ({ o, t: qnorm(o) }));
  if (opts.length < 2 || opts.some((x) => MOVE_TERMS.test(x.t))) return null;
  const move = opts.filter((x) => MOVE_OPTION.test(x.t) && !NO_MOVE.test(x.t));
  if (move.length !== 1) return null;
  let local = opts.filter((x) => x !== move[0] && LOCAL_OPTION.test(x.t) && !/\b(do not|dont|don t|not) (currently )?(live|reside|located|based|local)\b/.test(x.t));
  // A plain Yes beside "Yes, but require relocation" is the Yes without a move.
  if (local.length === 0) local = opts.filter((x) => x !== move[0] && /^yes\W*$/.test(x.t));
  // Living there twice over, on site or remote only ("I currently live in one
  // of those cities AND I am willing to work on-site." | "…, but I am only
  // open to remote opportunities.", Artera on Lever, live 2026-10-08): the
  // remote one only for someone who said remote.
  if (local.length > 1) {
    const remote = /^remote$/i.test((profile.workPreference ?? "").trim());
    const onlyRemote = (x: { t: string }): boolean => /\b(only (open to|interested in) remote|remote only|only remote)\b/.test(x.t);
    local = local.filter((x) => onlyRemote(x) === remote);
  }
  if (local.length !== 1) return null;
  const no = opts.filter((x) => x !== move[0] && x !== local[0] && optionPolarity(x.o) === false && !LOCAL_OPTION.test(x.t));
  // Every place named counts: "…Santa Barbara, Kansas City, or the Seattle
  // metropolitan area" is a Yes for Seattle.
  const named = placesIn(local[0].o).length ? placesIn(local[0].o) : placesIn(q.label);
  const places: NamedPlace[] = named.length ? named : ctx.jobCity ? [{ kind: "city", name: ctx.jobCity }] : [];
  if (places.length === 0) return abstain("local-or-move:no-place");
  const area = AREA_WORDS.test(`${local[0].t} ${qnorm(q.label)}`);
  const verdicts = places.map((p) => livesAt(p, facts, area).lives);
  let lives: boolean | null = verdicts.some((v) => v === true) ? true : verdicts.every((v) => v === false) ? false : null;
  const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
  if (lives === null && ctx.jobCountry && home && home !== ctx.jobCountry) lives = false;
  if (lives === true) return answer(local[0].o, "local-or-move:lives-there");
  if (lives === null) return abstain("local-or-move:unknown");
  const willing = polarityOf(profile.willingToRelocate || "");
  if (willing === true) return answer(move[0].o, "local-or-move:moving");
  if (willing === false) return no.length === 1 ? answer(no[0].o, "local-or-move:staying") : abstain("local-or-move:no-staying-option");
  return abstain("local-or-move:relocation-unknown");
}

// ----- Places as choices ------------------------------------------------------

function renderCountry(country: Country, q: QuestionInput, rule: string): QuestionResult {
  const opts = q.options ?? [];
  if (opts.length === 0) return answer(country.name, rule);
  const hits = opts.filter((o) => {
    const c = countryFromName(o.replace(/\s*\(.*\)\s*$/, "")) ?? countryFromName(o);
    return c?.code === country.code;
  });
  if (hits.length === 1) return answer(hits[0], rule);
  const direct = pickOption(opts, country.name);
  if (direct) return answer(direct, rule);
  // Not listed: the list's own "Other" (a German citizen among "United States
  // | Australia | Canada | New Zealand | United Kingdom | Other").
  const other = opts.filter((o) => /^other\b/i.test(o.trim()));
  return other.length === 1 && hits.length === 0 ? answer(other[0], `${rule}:other`) : abstain(`${rule}:no-matching-option`);
}

const OUTSIDE_OPTION = /\b(not applicable|n a|none|other|outside|international|non us|not in the|i do not (live|reside)|not a us|foreign|not listed)\b|^na$/;

/** Places under US embargo or sanctions, as export-control questions list them. */
const SANCTIONED = /\b(cuba|iran|north korea|syria|crimea|donetsk|luhansk|zaporizhzhia|kherson|russia|belarus|venezuela|sudan)\b/;

/**
 * Export-control lists of embargoed places ("Citizen or permanent resident of
 * Cuba, Iran, North Korea, or Syria" | … | "None of the above", Databricks;
 * "Do you reside in … any of the following countries: Cuba, Iran…?", Planet
 * Labs; question bank 2026-10-05): the applicant's own countries (citizenship,
 * residence, permanent residence) against every place listed. None named:
 * "None of the above" / No. One named, or the citizenship unknown: blank.
 */
function resolveSanctionedList(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  const opts = (q.options ?? []).filter((o) => o.trim() && !/^(select|choose|please select)/i.test(o.trim()));
  const listed = opts.filter((o) => SANCTIONED.test(qnorm(o)));
  const none = opts.filter((o) => /^none( of the above| of these| apply| applies)?\W*$/i.test(o.trim()));
  const checklist = listed.length > 0 && none.length === 1 && opts.every((o) => o === none[0] || SANCTIONED.test(qnorm(o)));
  // "Are you a citizen or legal permanent resident of Cuba, Iran, North Korea,
  // Syria or Ukraine (Crimea region)?" lists them itself (Asana, Intercom;
  // question bank 2026-10-05). A question about a country that is NOT one of
  // them ("an ADDITIONAL citizenship of any other country that is NOT Iran…")
  // is another question.
  const listsThem = new Set(n.match(new RegExp(SANCTIONED.source, "g")) ?? []).size >= 2;
  const yesNo =
    !checklist &&
    SANCTIONED.test(n) &&
    isBooleanQuestion(q) &&
    (/\b(any of the following|the following (countries|territories|regions))\b/.test(n) || listsThem) &&
    !/\b(not|other than|except|besides)\s+(in\s+)?(the\s+)?(cuba|iran|north korea|syria|russia)\b/.test(n);
  if (!checklist && !yesNo) return null;
  const residence = isHigh(facts.location.country) ? facts.location.country.value : null;
  const held = [...facts.workAuth.byCountry.entries()];
  const citizenOf = held.filter(([, a]) => a.basis === "citizen").map(([cc]) => countryByCode(cc));
  const prOf = held.filter(([, a]) => a.basis === "permanent_resident").map(([cc]) => countryByCode(cc));
  // Only the facts the question asks about: "Do you reside in…" needs no
  // citizenship (Planet Labs).
  const asked = checklist ? opts.join(" ") : q.label;
  const asksCitizenship = /\b(citizens?|citizenship|nationals?|nationality|passport)\b/i.test(asked);
  const asksResidence = /\b(reside|resides|resident|residence|live|living|located|ordinarily)\b/i.test(asked) || !asksCitizenship;
  if ((asksResidence && !residence) || (asksCitizenship && citizenOf.length === 0)) return abstain("sanctions:unknown");
  const mine = [...(asksResidence ? [residence, ...prOf] : []), ...(asksCitizenship ? citizenOf : [])].filter((c): c is Country => Boolean(c));
  // Codes that are also words never name a country in a sentence: Canada's
  // "can" was found in "Can you confirm…" (Ross Video, question bank 2026-10-17).
  const names = (c: Country): string[] => [c.name, ...c.aliases].map(qnorm).filter((w) => w.length > 2 && w !== "can" && w !== "nor");
  const named = (text: string): boolean => {
    const t = ` ${qnorm(text)} `;
    return mine.some((c) => names(c).some((w) => t.includes(` ${w} `)));
  };
  if (checklist) return listed.some(named) ? abstain("sanctions:named") : answer(none[0], "sanctions:none");
  const noUs = (text: string): string => text.replace(/\b(u\.?\s?s\.?|us|united states)\s+(export|government|laws?|regulations?|sanctions|embargo)\b/gi, " ");
  // "…restrictions for individuals from countries under U.S. embargo,
  // including Cuba, Iran, Syria, or North Korea. Can you confirm that you meet
  // these requirements and are eligible to comply?" (Ross Video on Jobvite,
  // question bank 2026-10-17): the places come first and the applicant
  // confirms they are not from them, so its Yes is this rule's "not one of
  // them". It was answered No for a US citizen.
  const sentence = qnorm(askedSentenceOf(q.label));
  if (/\b(confirm|certify|attest|affirm)\b.*\b(meet|comply|satisfy|eligible)\b|\b(do|can|will) you (meet|comply with|satisfy)\b/.test(sentence)) {
    return named(noUs(q.label)) ? abstain("sanctions:named") : booleanResult(true, q, "sanctions:confirmed");
  }
  // The places asked about are in the sentence asked; "U.S. export control
  // laws" before it names no place (a US citizen read as listed, Asana).
  return named(noUs(askedSentenceOf(q.label))) ? abstain("sanctions:named") : booleanResult(false, q, "sanctions:none");
}

/**
 * "Do you plan to move out of the state/country in which you currently reside
 * within the next 6-12 months?" over "I have no plans to move at this time"
 * and a list of states (Squarespace, question bank 2026-10-05): a plan, and it
 * was answered with the state the applicant lives in. Someone who will not
 * relocate has none; anyone else's plans are their own to state.
 */
function resolveMovePlan(q: QuestionInput, n: string, profile: UserApplicationProfile): QuestionResult {
  if (!/\b(plan|plans|planning|intend|intending|expect|expecting)( on)? (to )?(move|moving|relocate|relocating)\b/.test(n)) return null;
  // A move in general, not one for this job (the relocation questions).
  if (!/\b(out of|away from|leave|leaving)\b|\b(with)?in the next\b/.test(n) || /\bfor (this|the) (role|position|job|opportunity)\b/.test(n)) return null;
  if (polarityOf(profile.willingToRelocate || "") !== false) return abstain("move-plan:unknown");
  if (isBooleanQuestion(q)) return booleanResult(false, q, "move-plan:none");
  const none = (q.options ?? []).filter((o) => /\bno plans?\b|\bnot (planning|intending)\b|^no\b/.test(qnorm(o)));
  return none.length === 1 ? answer(none[0], "move-plan:none") : abstain("move-plan:unknown");
}

/** The Middle East, in EMEA though its countries are listed under Asia. */
const MIDDLE_EAST = new Set(["TR", "IL", "AE", "SA", "QA", "KW", "BH", "OM", "JO", "LB", "EG"]);

/** An option naming a business region the country is in: "EMEA", "APAC",
 *  "LATAM", "Americas", "Europe", "Asia Pacific", "North America". */
function inWorldRegion(c: Country, option: string): boolean {
  const me = MIDDLE_EAST.has(c.code);
  if (/^(emea|europe middle east (and|&)? ?africa)$/.test(option)) return c.continent === "Europe" || c.continent === "Africa" || me;
  if (/^(apac|asia pacific|asia and pacific|asia)$/.test(option)) return (c.continent === "Asia" && !me) || c.continent === "Oceania";
  if (/^(latam|latin america|south america|central (and|&) south america)$/.test(option)) return c.continent === "South America" || c.code === "MX";
  if (/^(amer|americas|the americas|north america|na)$/.test(option)) return c.continent === "North America" || (option.includes("americas") && c.continent === "South America");
  if (/^europe$/.test(option)) return c.continent === "Europe";
  if (/^(africa)$/.test(option)) return c.continent === "Africa";
  if (/^middle east$/.test(option)) return me;
  return false;
}

/** "Which state do you reside in?" / a State or Province select. */
function resolveRegionChoice(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  // "Please select the state where you will reside and work" (Calendly,
  // question bank 3) too.
  const asksRegion = q.category === "addressState" || /\b(which|what) (state|province)\b|\bstate (or province )?(of|you) (residence|reside|live)\b|\bprovince of residence\b|\bstate region (in which|where) you (currently )?(reside|live)\b|\b(state|province) (where|in which) you (will )?(currently )?(reside|live|work)\b/.test(n);
  if (!asksRegion || !q.options || q.options.length < 3) return null;
  const loc = facts.location;
  if (isHigh(loc.region)) {
    const r = loc.region.value;
    const hit = q.options.filter((o) => {
      const reg = regionFromText(o.replace(/\s*\(.*\)\s*$/, ""));
      return reg?.code === r.code && reg.country === r.country;
    });
    if (hit.length === 1) return answer(hit[0], "region-choice");
  }
  // Buckets beside the states ("New York | Illinois | Another State in the
  // US | APAC | EMEA | Other", Waymo, question bank 2026-10-05): a US
  // applicant's unlisted state, or the applicant's part of the world.
  if (isHigh(loc.country)) {
    const c = loc.country.value;
    if (c.code === "US" && isHigh(loc.region)) {
      const another = q.options.filter((o) => /\b(another|other|different)( us| u s)? states?\b/.test(qnorm(o)) && !/\b(outside|non|not in|international)\b/.test(qnorm(o)));
      if (another.length === 1) return answer(another[0], "region-choice:another-state");
    }
    const bucket = q.options.filter((o) => inWorldRegion(c, qnorm(o)));
    if (bucket.length === 1) return answer(bucket[0], "region-choice:world-region");
  }
  // The options are another country's regions: pick the explicit "not
  // applicable / outside" option, never a real state.
  const optionCountries = new Set(q.options.map((o) => regionFromText(o)?.country).filter(Boolean));
  if (isHigh(loc.country) && optionCountries.size === 1 && !optionCountries.has(loc.country.value.code as "US" | "CA")) {
    const outside = q.options.filter((o) => OUTSIDE_OPTION.test(qnorm(o)) && !regionFromText(o));
    if (outside.length === 1) return answer(outside[0], "region-choice:outside");
    return abstain("region-choice:outside-not-offered");
  }
  return isHigh(loc.region) ? abstain("region-choice:no-matching-option") : abstain("region-choice:unknown");
}

/** "Where are you located?" with region/continent options ("North America"). */
function resolveLocatedChoice(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\bwhere (are you|do you) (currently )?(located|based|live|reside)\b|\b(current|your) location\b|\bregion of residence\b|\bare you (currently )?(living|located|based|residing) in\b/.test(n)) return null;
  // A preference ("preferred work location", "which office") is not a residence.
  if (/\b(prefer|preferred|desired|willing|office|work location|would you like)\b/.test(n)) return null;
  if (!q.options || q.options.length < 2 || isBooleanOptionSet(q.options)) return null;
  const loc = facts.location;
  // A state or province named whole among the places: "Where do you currently
  // live?" over US states and a few countries (Squarespace, Greenhouse bank,
  // 2026-10-08), blank for everyone while only cities, countries and
  // continents were matched.
  const regionOptions = q.options.filter((o) => {
    const reg = regionFromText(o.trim());
    return reg !== null && (geoNorm(o) === geoNorm(reg.name) || o.trim() === reg.code);
  });
  // The options must be places at the scale we can judge: countries,
  // continents, or states and provinces.
  const placeLike = q.options.filter((o) => countryNamedIn(o) || /\b(north|south|latin|central) america|europe|asia|africa|oceania|emea|apac|latam\b/i.test(o));
  if (placeLike.length === 0 && regionOptions.length < 3) return null;
  if (!isHigh(loc.country)) return abstain("located-choice:unknown");
  const country = loc.country.value;
  const city = isHigh(loc.city) ? geoNorm(loc.city.value) : null;
  // Most specific first: the city, then the state, the country, the continent.
  if (city) {
    const byCity = q.options.filter((o) => ` ${geoNorm(o)} `.includes(` ${city} `));
    if (byCity.length === 1) return answer(byCity[0], "located-choice:city");
  }
  if (isHigh(loc.region)) {
    // The state alone ("Washington") or with its country ("US - Washington",
    // "Canada - Ontario", Dropbox, Greenhouse bank 3): every part of the option
    // is the applicant's country or region, so "US - Washington, D.C" is not.
    const r = loc.region.value;
    const byRegion = q.options.filter((o) => {
      const parts = o.split(/\s+[-\u2013]\s+|\s*[,/|()]\s*/).map((s) => s.trim()).filter(Boolean);
      let named = false;
      for (const part of parts) {
        const reg = regionFromText(part);
        if (reg && reg.code === r.code && reg.country === r.country && (geoNorm(part) === geoNorm(reg.name) || part === reg.code)) named = true;
        else if (countryFromName(part)?.code !== country.code) return false;
      }
      return named;
    });
    if (byRegion.length === 1) return answer(byRegion[0], "located-choice:region");
  }
  const byCountry = q.options.filter((o) => {
    const c = countryNamedIn(o);
    return c && c !== "this-country" && c.code === country.code;
  });
  if (byCountry.length === 1) return answer(byCountry[0], "located-choice:country");
  // "United States" beside "United States Minor Outlying Islands" and "Virgin
  // Islands, U.S." (Hermeus on Lever, live 2026-10-03): the option that IS the
  // country, not one that names it.
  const itself = byCountry.filter((o) => countryFromName(o.trim())?.code === country.code);
  if (itself.length === 1) return answer(itself[0], "located-choice:country");
  const continent = geoNorm(country.continent);
  const byContinent = q.options.filter((o) => geoNorm(o) === continent || geoNorm(o).includes(continent));
  if (byContinent.length === 1) return answer(byContinent[0], "located-choice:continent");
  // None of the places is the applicant's: the list's own "not listed" option,
  // when every other option is a place judged above (a metro name such as
  // "San Francisco Bay Area" could hold the applicant's city, so it stops it).
  // Never when the list names the applicant's country at all ("Canada -
  // Alberta" beside a missing Ontario): then they are somewhere listed.
  const unlisted = q.options.filter((o) => /\bnot listed\b|^other\b|\bnone of the (above|listed)\b/.test(qnorm(o)));
  const judged = q.options.every((o) => unlisted.includes(o) || regionOptions.includes(o) || placeLike.includes(o));
  if (unlisted.length === 1 && judged && byCountry.length === 0) return answer(unlisted[0], "located-choice:not-listed");
  return abstain("located-choice:no-matching-option");
}

// ----- Age -----------------------------------------------------------------

const AGE_MIN = /\b(?:at least|minimum(?: age)?(?: of)?|older than|over(?: the age of)?|above)\s+(\d{1,2})\b|\b(\d{1,2})\s*(?:\+|years? of age|years? old|or older|or above|or over|and older|and over)/;
const AGE_UNDER = /\b(?:under|younger than|below|less than)\s+(?:the age of\s+)?(\d{1,2})\b/;

function resolveAge(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  const ageWord = /\bage\b|\bold\b|\bolder\b|\byears of age\b/.test(n);
  const under = AGE_UNDER.exec(n);
  const min = under ? null : AGE_MIN.exec(n);
  if ((under || min) && ageWord) {
    const threshold = Number((under ?? min)![1] ?? (under ?? min)![2]);
    if (threshold < 13 || threshold > 80) return null;
    if (!isBooleanQuestion(q)) return null;
    if (!facts.age) return abstain("age-gate:no-dob");
    const [youngest, oldest] = facts.age;
    let meets: boolean | null = null;
    if (youngest >= threshold) meets = true;
    else if (oldest < threshold) meets = false;
    if (meets === null) return abstain("age-gate:straddles");
    return booleanResult(under ? !meets : meets, q, "age-gate");
  }
  if (/\b(what is your age|your age|age range|how old are you|age group)\b/.test(n)) {
    if (!facts.age || facts.age[0] !== facts.age[1]) return abstain("age:no-exact-dob");
    const age = String(facts.age[0]);
    if (q.options && q.options.length) {
      const hit = pickOption(q.options, age);
      return hit ? answer(hit, "age:bucket") : abstain("age:no-matching-option");
    }
    return answer(age, "age:value");
  }
  return null;
}

// ----- Experience ------------------------------------------------------------

const GENERIC_QUALIFIERS = new Set([
  "relevant", "professional", "work", "working", "total", "overall", "paid", "full", "time", "fulltime",
  "industry", "related", "post", "graduate", "postgraduate", "of", "the", "your", "prior", "previous", "practical", "hands", "on",
]);

/** Words naming a field of work, and the title words that count as that field. */
const DOMAINS: Array<{ q: RegExp; title: RegExp }> = [
  { q: /\b(software|programming|coding|development|developer|engineering|engineer|technical|technology|it)\b/, title: /\b(software|developer|engineer|engineering|programmer|swe|sde|full ?stack|front ?end|back ?end|devops|data|machine learning|ml|web|mobile|platform|systems)\b/ },
  { q: /\b(data|analytics|analysis|analyst)\b/, title: /\b(data|analyst|analytics|scientist|bi)\b/ },
];

function resolveYearsOfExperience(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  const m = /\b(?:years?|yrs)\s+(?:of\s+)?([a-z\s-]{0,60}?)\s*experience\b|\bhow (?:much|many years of)\s+([a-z\s-]{0,60}?)\s*experience\b|\bexperience \(years\)|\bexperience in years\b/.exec(n);
  if (!m) return null;
  const qualifiers = (m[1] ?? m[2] ?? "").split(/[\s-]+/).filter(Boolean);
  const domainWords = qualifiers.filter((w) => !GENERIC_QUALIFIERS.has(w));
  // "Years of experience WITH React", "…experience do you have IN marketing",
  // "…AS a nurse": what follows narrows the question. A generic object ("in
  // the industry", "in a professional setting") keeps it the career total; a
  // field of work joins the domain check below; anything else (a skill, a
  // tool, a product) is a question the profile cannot answer.
  // An activity may come between ("…experience do you have developing
  // applications using C# and ASP.NET Core?", Vagaro on Breezy, live
  // 2026-10-03: answered "3+ years" from six years of teaching).
  const narrowed =
    /\bexperience\b(?:\s+(?:do|did|have|has|you|of|that|which|would|say|in total)){0,5}(?:\s+[a-z]+ing(?:\s+[a-z]+){0,3}?)?\s+(?:with|in|using|on|as|doing|working with|working in|leading|managing)\s+(?:a |an |the )?([a-z0-9 +#.-]{2,40})/.exec(n);
  // An activity right after it narrows it too: "…experience performing system
  // administration of Microsoft Windows Server…", "…experience do you have
  // owning customer implementations…", "…your experience servicing industrial
  // equipment" (Saalex, JeffreyM, Smartflower on Workable, 2026-10-05) all
  // got the career total.
  const activity = narrowed
    ? null
    : /\bexperience\b(?:\s+(?:do|did|have|has|you|of|that|which|would|say|in total)){0,5}\s+([a-z]+ing)\s+((?:[a-z0-9+#.-]+\s*){1,4})/.exec(n);
  // A count no career reaches is No whatever it narrows to: the specific
  // experience is part of the whole. The whole as dated rows, never a stated
  // figure (a bootcamp graduate's "3" counts only the new career).
  const short = (): QuestionResult => {
    const total = facts.employment.datedYears;
    const need = /\b(at least|minimum( of)?|more than|over|greater than)?\s*(\d+(?:\.\d+)?)\s*\+?\s*(or more\s+)?(years?|yrs)\b/.exec(n);
    if (!isBooleanQuestion(q) || !need || !isHigh(total)) return null;
    const strict = /more than|over|greater than/.test(need[1] ?? "");
    return (strict ? total.value <= Number(need[3]) : total.value < Number(need[3])) ? booleanResult(false, q, "years-experience:total-below") : null;
  };
  if (narrowed || (activity && !/^(working|having|being)$/.test(activity[1]))) {
    const obj = narrowed ? narrowed[1].trim() : `${activity![1]} ${activity![2]}`.trim();
    if (!/^(total|industry|the industry|professional (setting|capacity|environment)s?|similar roles?|this field|the field|related fields?|the workforce|a professional|full time|paid)\b/.test(obj)) {
      domainWords.push(...obj.split(/\s+/).slice(0, 3));
      const dom = DOMAINS.find((d) => d.q.test(obj));
      if (!dom) return short() ?? abstain("years-experience:narrowed");
    }
  }
  // A stack in parentheses after the experience narrows it too: "…fullstack
  // software engineering experience do you have (Python/React or Vuejs)?"
  // (AlayaCare, question bank 2026-10-05) got the career total.
  const paren = /\bexperience\b[^?(]*\(([^)]{2,80})\)/i.exec(q.label);
  if (paren && !/^\s*(in )?(years?|total|approx\w*|overall)\b/i.test(paren[1])) return abstain("years-experience:narrowed");
  const total = facts.employment.totalYears;
  if (!isHigh(total)) return abstain("years-experience:unknown");
  if (domainWords.length > 0) {
    const phrase = domainWords.join(" ");
    const dom = DOMAINS.find((d) => d.q.test(phrase));
    const titles = facts.employment.titles;
    if (!dom || titles.length === 0 || !titles.every((t) => dom.title.test(t.toLowerCase()))) {
      return abstain("years-experience:domain-unproven");
    }
    // A specialty inside the field ("fullstack", "front end", "mobile") is
    // proven only by titles that name it. Support work too: "experience in
    // desktop or IT support roles" got a software career's years (Wintermute
    // on Lever, question bank 2026-10-08).
    const specialty = /\b(full ?stack|front ?end|back ?end|mobile|ios|android|embedded|devops|machine learning|security|cloud|qa|support|help ?desk|desktop)\b/.exec(phrase);
    if (specialty && !titles.every((t) => new RegExp(`\\b${specialty[1].replace(/ /g, " ?")}\\b`, "i").test(t.toLowerCase().replace(/-/g, " ")))) {
      return abstain("years-experience:specialty-unproven");
    }
  }
  const years = total.value;
  if (isBooleanQuestion(q)) {
    // "Do you have at least 3 years of experience?" / "3+ years" / "more than 5 years"
    // "1-2 years of experience" (FSSI on Workable, live 2026-10-03, answered
    // NO for 1.4 years): a range is met from its lower bound.
    // (Normalized, "1–2 years" reads "1 2 years".)
    const range = /\b(\d+(?:\.\d+)?)\s*(?:-|to|\s)\s*(\d+(?:\.\d+)?)\s*\+?\s*(years?|yrs)\b/.exec(n);
    if (range) return booleanResult(years >= Number(range[1]), q, "years-experience:range");
    // "under 2 years" is the inverse of "at least 2" (Veeva on Lever: its N/A
    // option "I have more than 2 years" was picked for 1.4 years).
    const under = /\b(under|less than|fewer than|below)\s+(\d+(?:\.\d+)?)\s*(years?|yrs)\b/.exec(n);
    if (under) return booleanResult(years < Number(under[2]), q, "years-experience:under");
    const need = /\b(at least|minimum( of)?|more than|over|greater than)?\s*(\d+(?:\.\d+)?)\s*\+?\s*(or more\s+)?(years?|yrs)\b/.exec(n);
    if (!need) return abstain("years-experience:boolean-no-threshold");
    const threshold = Number(need[3]);
    const strict = /more than|over|greater than/.test(need[1] ?? "");
    return booleanResult(strict ? years > threshold : years >= threshold, q, "years-experience:threshold");
  }
  if (q.options && q.options.length) {
    const hit = pickOption(q.options, String(years));
    return hit ? answer(hit, "years-experience:bucket") : abstain("years-experience:no-matching-option");
  }
  return answer(String(Math.floor(years)), "years-experience:value");
}

// ----- Education ---------------------------------------------------------------

// "Your most recently completed form of education" too (NISC, question bank
// 2026-10-05: the bachelor's still in progress was written).
const LEVEL_Q = /\bhighest (completed )?(level of )?(education|degree|qualification|educational|schooling)\b|\b(level|type|form) of (education|degree)\b|\beducation(al)? level\b|\bdegree level\b|\bhighest education\b|\bmost recent(ly)? completed (education|degree|qualification)\b/;

/** Rank an OPTION (labels like "Bachelors", "Associates", "GED", "PhD"). */
function optionRank(o: string): number | null {
  const t = qnorm(o);
  // "Some High School" is high school unfinished (Lucid Motors, question bank
  // 2026-10-17: a bootcamp graduate with a diploma got it). Below any rank a
  // profile holds: 0 is a student's "nothing completed yet", and a Waterloo
  // student got "Less than high school or equivalent" (Wattpad) by it.
  if (/\b(some|less than|no|partial|incomplete) (high school|secondary)\b|\bdid not (complete|finish|graduate)\b/.test(t)) return -1;
  // ("HS Diploma", GEI on Jobvite: its "diploma" ranked it a college one.)
  if (/\bged\b|\bhigh school\b|\bhs\b|\bsecondary\b/.test(t)) return 1;
  if (/\bassociates?\b/.test(t)) return 3;
  if (/\bbachelors?\b|\bundergrad/.test(t)) return 4;
  if (/\bmasters?\b|\bmba\b/.test(t)) return 5;
  if (/\bphd\b|\bdoctor(ate|al)?\b/.test(t)) return 6;
  return degreeRank(o);
}

function resolveEducationLevel(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  const ed = facts.education;
  // "Do you have / have you completed a bachelor's degree?". "Do you currently
  // hold a minimum of a Bachelor's degree…" and "Do you meet the education
  // requirements, a bachelor's degree minimum?" too (Avalore, Credence on
  // Workable, 2026-10-05: blank, and Yes for a student and a bootcamp
  // graduate as an accepted requirement).
  // Experience is no degree: "Do you have experience working with
  // authentication protocols… implementing RBAC and MFA…?" (Fullscript on
  // Lever, question bank 2026-10-08) read MFA as a Master of Fine Arts.
  const experience = /\bexperience\b/.test(n) && !/\b(degree|diploma)\b/.test(n);
  const asked =
    !experience && /\b(do you (currently |presently |already )?(have|hold|possess)|have you (completed|obtained|earned|received|attained)|did you (complete|earn|obtain))\b|\bdo you meet (the |our |this )?(e\w*cation(al)?|degree|academic) (requirements?|qualifications?|criteria)\b/.test(n)
      ? /\b(hs|high school|secondary school|ged)\b/.test(n) ? 1 : degreeRank(n) ?? optionRank(n)
      : null;
  if (asked && isBooleanQuestion(q)) {
    if (/\b(in|related|relevant) (to )?(a |the )?(computer|engineering|stem|related field|technical|science|business)\b|\bfield\b|\bmajor\b/.test(n)) {
      return abstain("degree-held:field-qualified");
    }
    const pursuing = /\b(or (are )?(you )?(currently )?(pursuing|enrolled|working towards|working toward|completing)|in progress|expected)\b/.test(n);
    // Anyone in or past a degree finished high school: "Do you have a HS
    // Diploma or GED?" got No from a university student (Saalex on Workable).
    const past = ed.entries.some((x) => x.rank !== null && x.rank >= 3) ? 1 : 0;
    const held = pursuing ? ed.highestRank : ed.highestCompletedRank;
    const rank = past && asked <= past && (!isHigh(held) || held.value < past) ? { value: past, confidence: "high" as const, source: "education:degree-implies-high-school" } : held;
    if (isHigh(rank)) return booleanResult(rank.value >= asked, q, "degree-held");
    if (!pursuing && isHigh(ed.highestRank) && ed.highestRank.value < asked) return booleanResult(false, q, "degree-held:below");
    return abstain("degree-held:unknown");
  }
  if (!LEVEL_Q.test(n)) return null;
  // The school of a degree is no level: "At which institution did you earn
  // your highest degree?" took "The Master's College" from a list of schools
  // (National Journal on Lever, question bank 2026-10-08).
  if (/\b(which|what) (institution|school|university|college)\b|\b(name of|where did you) (the |your )?(institution|school|university|college|earn|get|receive|obtain|complete)\b/.test(n)) return null;
  // Nor is its subject: "In which area of study was the major coursework of
  // your highest level of education?" (City and County of San Francisco,
  // SmartRecruiters bank, round 7) got "Master's Degree" and "Diploma".
  if (/\b(area|field|subject|discipline|major|concentration|speciali[sz]ation)s? of (study|studies)\b|\bmajor coursework\b|\b(what|which) (was |is )?(your |the )?(major|discipline|subject|concentration)\b/.test(n)) return null;
  const completedAsked = /\b(completed|attained|obtained|achieved|earned)\b/.test(n);
  // "What is your current degree program or highest level of education?"
  // (Enfos on Workable, 2026-10-05): a degree in progress answers it. So does
  // "your current or most recently completed degree type" (Resultant,
  // SmartRecruiters bank, round 7), first of all.
  const programAsked = /\bcurrent (degree|program|degree program|studies|course of study)\b|\bcurrent or (most )?recent(ly completed)? (degree|program|education)\b/.test(n);
  const rank = programAsked && isHigh(ed.highestRank) ? ed.highestRank : ed.highestCompletedRank;
  // A degree in progress with none completed past high school: "Some College"
  // is the level held under either reading of "highest" (a bachelor's
  // student on ActioNet's Jobvite form, a real profile, 2026-10-03). Only
  // when the list offers it.
  const studying = ed.entries.some((x) => x.completed === false && x.rank !== null && x.rank >= 3);
  if (studying && (!isHigh(rank) || rank.value <= 1) && q.options?.length) {
    const some = q.options.filter((o) => /\bsome (college|university|post ?secondary)\b/.test(qnorm(o)));
    if (some.length === 1) return answer(some[0], "education-level:some-college");
    // Without it, the level COMPLETED is the high school diploma a degree
    // program implies: "Please confirm your highest level of education
    // completed." (EVERSANA), "…the highest level of education you have
    // obtained to date?" (M3USA); SmartRecruiters bank, round 7: blank for a
    // Waterloo student.
    if (completedAsked && !programAsked) {
      const school = q.options.filter((o) => optionRank(o) === 1);
      const diploma = school.filter((o) => !/\bged\b/.test(qnorm(o)) || /\bhigh school\b|\bhs\b|\bsecondary\b/.test(qnorm(o)));
      const pick = diploma.length === 1 ? diploma : school;
      if (pick.length === 1) return answer(pick[0], "education-level:program-implies-high-school");
    }
  }
  // Highest level with something still in progress above it: "completed"
  // questions take the completed level; ambiguous wording abstains.
  if (!isHigh(rank)) return abstain("education-level:unknown");
  if (!completedAsked && isHigh(ed.highestRank) && ed.highestRank.value > rank.value) return abstain("education-level:in-progress-above");
  if (!q.options || q.options.length === 0) {
    const label = [, "High School", "Diploma", "Associate Degree", "Bachelor's Degree", "Master's Degree", "Doctorate"][rank.value];
    return label ? answer(label, "education-level:value") : abstain("education-level:unknown");
  }
  const hits = q.options.filter((o) => optionRank(o) === rank.value);
  if (hits.length === 1) return answer(hits[0], "education-level");
  // "High School" vs "GED", "Master's" vs "MBA": prefer the generic one.
  const generic = hits.filter((o) => !/\b(ged|mba|m b a|md|jd)\b/.test(qnorm(o)));
  if (generic.length === 1) return answer(generic[0], "education-level");
  // Of a level's kinds, the degree's own: "Master of Science" beside "Master
  // of Arts" for a Master of Science in Information Systems (Western Digital,
  // SmartRecruiters bank, round 7: blank).
  const own = ed.entries.filter((x) => x.rank === rank.value).map((x) => qnorm(x.degree));
  const KINDS = /\bof (science|arts|engineering|applied science|business administration|fine arts|education|technology)\b/;
  const kindOf = (t: string): string | null => KINDS.exec(t)?.[1] ?? null;
  const ownKinds = new Set(own.map(kindOf).filter((k): k is string => k !== null));
  if (ownKinds.size === 1) {
    const kind = [...ownKinds][0];
    const same = generic.filter((o) => kindOf(qnorm(o)) === kind);
    if (same.length === 1) return answer(same[0], "education-level:kind");
  }
  // A certificate or college diploma the list does not offer (a bootcamp
  // graduate on Nutanix's High School / Associate / … list, Jobvite bank
  // 2026-10-17): high school, which it implies, is the highest level listed.
  // A GED alone is not the diploma; "High School Diploma/GED" is.
  if (hits.length === 0 && rank.value === 2) {
    const school = q.options.filter((o) => optionRank(o) === 1);
    const diploma = school.filter((o) => !/\bged\b/.test(qnorm(o)) || /\bhigh school\b|\bhs\b|\bsecondary\b/.test(qnorm(o)));
    const pick = diploma.length === 1 ? diploma : school;
    if (pick.length === 1) return answer(pick[0], "education-level:certificate-implies-high-school");
  }
  return abstain("education-level:no-unique-option");
}

/**
 * Class standing: "What is your current class standing?" [Freshman |
 * Sophomore | Junior | Senior | Other] (Bosch), "What year of your program are
 * you currently completing?" [1st Year | … | 4th Year or Above] (GDMSI);
 * SmartRecruiters bank, round 7; "Please indicate your level of education
 * during the Fall 2026 Semester:" (Gallup on Greenhouse, live). A bachelor's
 * student who graduates within the academic year asked about is in their
 * last: a senior, in their 4th year or above. Any earlier year turns on the
 * program's length, which no profile holds. A graduate student is the list's
 * graduate level; someone enrolled nowhere is none of them, where it says so.
 */
function resolveClassStanding(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  const opts = (q.options ?? []).filter((o) => o.trim());
  const t = opts.map((o) => qnorm(o));
  const isYear = (o: string): boolean => /^(freshman|sophomore|junior|senior)\b|^(1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth) year\b/.test(o);
  if (t.filter(isYear).length < 3) return null;
  if (!/\b(standing|year|level of education|academic level|classification|grade level)\b/.test(n)) return null;
  const pick = (re: RegExp): string | null => {
    const hits = opts.filter((_, i) => re.test(t[i]));
    return hits.length === 1 ? hits[0] : null;
  };
  // A graduate's option: "Graduated", "I will have graduated college/university"
  // (HAI Group on Greenhouse), "Alumni".
  const GRADUATED = /^(i )?(will have |have )?(already )?graduated\b|^alumn(i|us|a)\b/;
  const entries = facts.education.entries;
  const studying = entries.filter((x) => x.completed === false);
  if (studying.length === 0) {
    if (!(isHigh(facts.education.currentlyEnrolled) && facts.education.currentlyEnrolled.value === false)) return abstain("class-standing:unknown");
    // Only a college degree graduates someone from a class-standing list: a
    // bootcamp certificate and high school are no college.
    const college = entries.some((x) => x.completed === true && x.rank !== null && x.rank >= 3);
    const none = pick(/^(none of the above|not (currently )?(a student|enrolled)|not applicable|n a)\b/) ?? (college ? pick(GRADUATED) : null);
    return none ? answer(none, "class-standing:not-enrolled") : abstain("class-standing:not-enrolled");
  }
  if (studying.some((x) => x.rank !== null && x.rank >= 5)) {
    const grad = pick(/\bgraduate (level |school )?(student|program)\b|^graduate\b|\bmasters?\b/);
    return grad ? answer(grad, "class-standing:graduate") : abstain("class-standing:graduate");
  }
  const bachelor = studying.find((x) => x.rank === 4);
  if (!bachelor?.graduation) return abstain("class-standing:unknown");
  // The academic year asked: a term named ("the Fall 2026 Semester"), else now.
  // A summer the student is "rising into" a year from opens the academic year
  // after it ("In summer 2027, what year will you be rising into?", HAI Group
  // on Greenhouse).
  const term = /\b(fall|autumn|winter|spring|summer)\s+((?:19|20)\d{2})\b/.exec(n);
  const rising = /\b(rising|entering|going into|heading into)\b/.test(n);
  const today = facts.today;
  const start = term ? (/^(fall|autumn)$/.test(term[1]) || (term[1] === "summer" && rising) ? Number(term[2]) : Number(term[2]) - 1) : today.getUTCMonth() >= 7 ? today.getUTCFullYear() : today.getUTCFullYear() - 1;
  if (term) {
    // Graduated before the term begins: no year of it left (an April 2027
    // graduate was a "Senior" in summer 2027). A graduation the term's start
    // may fall either side of is theirs.
    const y = Number(term[2]);
    const begins = term[1] === "summer" ? Date.UTC(y, 5, 1) : /^(fall|autumn)$/.test(term[1]) ? Date.UTC(y, 8, 1) : Date.UTC(y, 0, 1);
    if (bachelor.graduation.latest.getTime() < begins) {
      const done = pick(GRADUATED);
      return done ? answer(done, "class-standing:graduated-by-term") : abstain("class-standing:graduated-by-term");
    }
    if (bachelor.graduation.earliest.getTime() < begins) return abstain("class-standing:graduation-month-unknown");
  }
  const yearEnds = Date.UTC(start + 1, 7, 31);
  if (bachelor.graduation.latest.getTime() > yearEnds) return abstain("class-standing:program-length-unknown");
  const last = pick(/^senior\b/) ?? pick(/^(4th|fourth) year (or|and) (above|higher|more|beyond)\b|^(4th|fourth) year\+/) ?? (t.some((o) => /^(5th|fifth) year\b/.test(o)) ? null : pick(/^(4th|fourth) year\b/));
  return last ? answer(last, "class-standing:final-year") : abstain("class-standing:no-unique-option");
}

/**
 * "Will you be returning to an undergraduate or graduate program after the
 * internship if selected?" (Everpure on Greenhouse, live 2026-10-17): a
 * graduate enrolled nowhere has no program to return to. A student's answer
 * turns on dates the profile does not settle (finishing before the
 * internship ends, or not), so it stays theirs.
 */
function resolveReturningToSchool(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\b(return|returning|go back|going back)\b[^?]{0,40}\b(school|program|programme|studies|university|college|degree|undergraduate|graduate)\b/.test(n)) return null;
  // "Following the completion of your internship, will you be returning to
  // school…?" (ServiceNow, SmartRecruiters, live, round 8) asks the same.
  if (!/\b(after|following|upon|on completion of|at the end of)\b[^?]{0,30}\b(internship|co ?op|placement|work term|term)\b/.test(n)) return null;
  if (!isBooleanQuestion(q)) return null;
  const e = facts.education.currentlyEnrolled;
  return isHigh(e) && !e.value ? booleanResult(false, q, "returning-to-school:graduated") : abstain("returning-to-school:student");
}

function resolveEnrollment(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  // ("Do you currently attend a college or university within an 80-mile
  // radius of San Francisco, CA?", Gallup on Greenhouse, live, round 7.)
  // ("Are you currently a University student in the Atlanta area pursuing an
  // Engineering degree?", SoloPulse on Lever, live, round 7.)
  if (!/\b(currently|presently) (a |an )?(full time |part time )?(university |college |undergraduate |graduate )?(student|enrolled|attending|attend|pursuing)\b|\bare you (a |an )?(current )?(full time |part time )?(university |college )?student\b|\benrolled (in|at)\b/.test(n)) return null;
  if (!isBooleanQuestion(q)) return null;
  // "...during the internship" / "...after graduation": another time, leave it.
  if (/\bduring\b|\bafter\b|\bthrough\b|\buntil\b/.test(n)) return abstain("enrollment:qualified");
  const e = facts.education.currentlyEnrolled;
  // "...enrolled at a Canadian institution" / "...attending a college or
  // university in the US?" (Superhuman, left to the AI): more than
  // enrollment, unless the applicant is enrolled nowhere.
  // A student "in the Atlanta area" too (decision 48: where a student's school
  // is, the profile does not say).
  if (/\b(institution|university|college|school) (in|of|located|within|near)\b|\bpost secondary institution\b|\b(canadian|american|us|u s|united states) (institution|university|college|school)\b|\b(student|students|enrolled|attending|attend)\b[^?]{0,15}\b(in|near|within|around) (the )?([a-z]+ ){1,3}(area|region|metro|radius)\b/.test(n)) {
    return isHigh(e) && !e.value ? booleanResult(false, q, "enrollment:enrolled-nowhere") : abstain("enrollment:qualified");
  }
  // "…enrolled in an accredited 4-year Bachelor's degree program or have
  // graduated within the past 2 years?" (Samsara, question bank 2026-10-05:
  // a June 2026 graduate said No): enrolled at that level, or finished it
  // that recently.
  const within = /\bor (have |has )?(graduated|completed)( \w+){0,3} (with)?in the (past|last) (\d+|one|two|three|four|five) (years?|months?)\b/.exec(n);
  if (within) {
    const count = /^\d+$/.test(within[6]) ? Number(within[6]) : ["one", "two", "three", "four", "five"].indexOf(within[6]) + 1;
    const since = new Date(facts.today.getTime());
    since.setUTCMonth(since.getUTCMonth() - (/month/.test(within[7]) ? count : count * 12));
    const asked = degreeLevelAsked(n);
    const fits = (x: { rank: number | null }) => asked === null || x.rank === asked;
    const entries = facts.education.entries;
    if (entries.some((x) => x.completed === false && fits(x))) return booleanResult(true, q, "enrollment:or-graduated-within");
    if (entries.some((x) => x.completed === true && fits(x) && x.graduation && x.graduation.latest >= since)) return booleanResult(true, q, "enrollment:or-graduated-within");
    // Every degree finished before the window, or at another level we can name.
    if (entries.length > 0 && entries.every((x) => x.completed === true && x.graduation && (x.graduation.latest < since || (x.rank !== null && !fits(x))))) {
      return booleanResult(false, q, "enrollment:or-graduated-within");
    }
    return abstain("enrollment:unknown");
  }
  // A field named with it ("…in Data Science, Computer Science, …,
  // Engineering, or another quantitative field", Ramp; "…in computer science
  // or another related field", Superhuman): a degree in progress outside the
  // sciences and engineering is the applicant's to judge.
  if (/\b(related|relevant|quantitative|technical|stem|similar|scientific) (field|discipline|area|major|program)s?\b/.test(n)) {
    const current = facts.education.entries.filter((x) => x.completed === false);
    if (current.length > 0 && !current.some((x) => STEM_DEGREE.test(qnorm(x.degree)))) return abstain("enrollment:field-unproven");
  }
  // "…enrolled in a PhD program…?" asks about THAT level (Neighbor on Lever,
  // live 2026-10-03: "Yes" for a bachelor's student). Several levels named
  // are any of them: "pursuing a Bachelor's, Master's, or PhD in…" (Ramp on
  // Ashby, live 2026-10-08) was read as the PhD alone, No for a bachelor's.
  const levels = degreeLevelsNamed(n);
  if (levels.length > 1) return programAtLevel(q, levels, facts, "enrollment:level");
  const level = degreeLevelAsked(n);
  if (level !== null) return programAtLevel(q, level, facts, "enrollment:level");
  // "…currently enrolled in OR have graduated from a university?": either one.
  if (/\bor (have |has )?(graduated|completed)\b|\bor (a )?(recent )?graduate\b/.test(n)) {
    if (isHigh(e) && e.value) return booleanResult(true, q, "enrollment:or-graduated");
    if (facts.education.entries.some((x) => x.completed === true)) return booleanResult(true, q, "enrollment:or-graduated");
    return abstain("enrollment:unknown");
  }
  if (isHigh(e)) return booleanResult(e.value, q, "enrollment");
  return abstain("enrollment:unknown");
}

/**
 * "Have you completed at least one previous internship? Please provide
 * details." (Hermeus on Lever, live 2026-10-03): the history's finished
 * internships and co-ops, named, or No.
 */
/**
 * "Are you a student in F-1 status who plans to work pursuant to CPT or OPT?"
 * (Cloudflare, question bank 2026-10-05): a visa question. Read as one about
 * enrollment, a Canadian citizen in school said Yes. F-1, OPT or CPT stated:
 * Yes (OPT is F-1 status); any other status stated: No.
 */
function resolveF1Status(q: QuestionInput, n: string, profile: UserApplicationProfile): QuestionResult {
  if (!/\bf ?1\b/.test(n) || !/\b(are|were) you\b/.test(n) || !isBooleanQuestion(q)) return null;
  const f1 = holdsF1Status(profile);
  return f1 === null ? abstain("f1-status:unknown") : booleanResult(f1, q, "f1-status");
}

/** F-1, OPT or CPT stated: true; any other status stated: false. */
function holdsF1Status(profile: UserApplicationProfile): boolean | null {
  const auth = qnorm(profile.workAuthorization ?? "");
  if (!auth) return null;
  if (/\bf ?1\b|\b(opt|cpt)\b|\bstudent visa\b/.test(auth)) return true;
  if (/\bcitizen|\bpermanent resident\b|\bgreen card\b|\bh ?1 ?b\b|\btn\b|\bl ?1\b|\be ?3\b|\bo ?1\b|\bh ?4\b|\bj ?1\b|\bpgwp\b|\bwork permit\b|\brefugee\b|\basylee\b/.test(auth)) return false;
  return null;
}

/**
 * "Does your work authorization now, or will it in the future, involve CPT
 * (Curricular Practical Training) or OPT (Optional Practical Training)?" (SEP
 * on Lever, question bank 2026-10-08) was read as "are you authorized?", and
 * an H-1B holder and a citizen said Yes. CPT and OPT are an F-1 student's.
 */
function resolvePracticalTraining(q: QuestionInput, n: string, profile: UserApplicationProfile): QuestionResult {
  // ("…reply STOP to opt out" is no OPT: Lucid's SMS opt-in, round 7.)
  if (!/\b(cpt|opt(?! (in|out)\b)|curricular practical training|optional practical training)\b/.test(n) || !isBooleanQuestion(q)) return null;
  // Holding it too: "Do you currently hold Optional Practical Training (OPT)
  // work authorization?", "Are you currently on OPT or CPT?" (Resultant on
  // SmartRecruiters, Profluent on Greenhouse; round 7) were read as "are you
  // authorized?", Yes for an H-1B holder and a citizen. Only held right
  // before it: "…temporary work authorization (e.g., OPT, CPT) and/or will
  // you require…" (Salesloft) and "…on an ongoing basis … such as OPT"
  // (Rave Aerospace) are other questions.
  const held = /\b(hold|holds|holding|on)( (a|an|the|your|current|currently|valid|active))* (stem opt|opt|cpt|optional practical training|curricular practical training)\b(?! (in|out)\b)/.test(n);
  if (!held && !/\b(involve|involves|include|includes|based on|rely on|relies on|through|pursuant to|under)\b/.test(n)) return null;
  const f1 = holdsF1Status(profile);
  // A question for those whose right rests on OPT "or another nonimmigrant
  // status" is not one for a citizen, a permanent resident or someone with
  // no US right: its "Not Applicable" ("If your current employment
  // eligibility is based on … OPT or another nonimmigrant status…, will you
  // be seeking Company support…?", National Life on Greenhouse, live, round
  // 7: No for a citizen). Another visa holder (H-1B) is asked: theirs.
  if (f1 === false && /^if (your|you)\b/.test(n)) {
    const na = (q.options ?? []).filter((o) => /^(not applicable|n\/?a|does not apply)\b/i.test(o.trim()));
    if (na.length === 1) {
      const stated = qnorm(profile.workAuthorization ?? "");
      if (/\bnon ?immigrant\b|\banother (visa|status)\b/.test(n) && /\bh ?1 ?b\b|\btn\b|\be ?3\b|\bl ?1\b|\bo ?1\b|\bh ?4\b|\bj ?1\b|\bvisa\b/.test(stated)) return abstain("practical-training:other-status");
      return answer(na[0], "practical-training:not-applicable");
    }
  }
  return f1 === null ? abstain("practical-training:unknown") : booleanResult(f1, q, "practical-training");
}

/** A job title in technical work: engineering, software, data, research. */
const TECH_TITLE =
  /\b(engineer(ing)?|developer|programmer|software|data|machine learning|ml|ai|devops|sre|architect|scientist|research(er)?|analyst|technical|technologist|it)\b/i;

function resolvePreviousInternship(q: QuestionInput, n: string, profile: UserApplicationProfile): QuestionResult {
  if (!/\b(have|did) you\b.*\b(completed|done|had|held|finished)\b.*\b(internships?|co ?ops?)\b/.test(n)) return null;
  const done = (profile.experience ?? []).filter(
    (e) => /\bintern(ship)?\b|\bco ?-?op\b/i.test(e.title ?? "") && Boolean(e.endDate?.trim()) && !/\b(present|current|now|ongoing)\b/i.test(e.endDate ?? "")
  );
  // "…at least 1 internship OR have relevant full-time experience?" (DoorDash,
  // question bank 2026-10-05): a staff engineer answered No. A full-time role
  // counts; "relevant" one only when it is technical work, which the profile
  // can show; otherwise relevance is the applicant's to judge.
  const orJob = /\bor (have |had )?(any )?(relevant )?(full ?time|professional|industry|work) (work |job )?experience\b/.exec(n);
  if (orJob && isBooleanQuestion(q) && done.length === 0) {
    const jobs = (profile.experience ?? []).filter((e) => (e.title ?? "").trim() && !/\bintern(ship)?\b|\bco ?-?op\b/i.test(e.title ?? ""));
    if (jobs.length === 0) return booleanResult(false, q, "internship:history");
    if (!/\brelevant\b/.test(orJob[0]) || jobs.some((e) => TECH_TITLE.test(e.title ?? ""))) return booleanResult(true, q, "internship:or-experience");
    return abstain("internship:relevance-unknown");
  }
  if (isBooleanQuestion(q)) return booleanResult(done.length > 0, q, "internship:history");
  if (q.kind !== "text" && q.kind !== "longText") return null;
  if (done.length === 0) return answer("No", "internship:none");
  const named = done.map((e) => `${e.title} at ${e.company}` + (e.startDate && e.endDate ? ` (${e.startDate} to ${e.endDate})` : ""));
  return answer(`Yes, ${named.join("; ")}`, "internship:history");
}

/**
 * "Do you have a school schedule that allows you to work part-time from our
 * Foster City office…?" (Zoox on Lever, live 2026-10-03: "Yes" for a developer
 * out of school). No school, no schedule: No. A student's timetable is theirs
 * to know; the AI could only guess it.
 */
function resolveSchoolSchedule(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\b(school|class|academic|course|university|college) (schedule|timetable)\b/.test(n) || !isBooleanQuestion(q)) return null;
  const entries = facts.education.entries;
  if (entries.length > 0 && entries.every((e) => e.completed === true)) return booleanResult(false, q, "school-schedule:not-enrolled");
  return abstain("school-schedule:unknown");
}

/**
 * "Are you available to participate in a full double-block co-op from January
 * 2027 through August 2027?" (Mindex on Workable, live 2026-10-03): a co-op is
 * a placement for enrolled students, so a graduate is not available for one.
 * A student's availability is the term's (resolvePeriodAvailability).
 */
function resolveCoop(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\bco ?op\b/.test(n) || !/\b(available|able|eligible) to (participate|complete|do|take part|join)\b|\bparticipate in\b/.test(n)) return null;
  if (!isBooleanQuestion(q)) return null;
  const entries = facts.education.entries;
  return entries.length > 0 && entries.every((e) => e.completed === true) ? booleanResult(false, q, "co-op:not-enrolled") : null;
}

/** The degree level a question names ("PhD program", "master's student"), as a rank. */
function degreeLevelAsked(n: string): number | null {
  if (/\b(phd|ph d|doctoral|doctorate)\b/.test(n)) return 6;
  if (/\b(masters?|graduate (program|student|degree)|mba)\b/.test(n) && !/\bundergraduate\b/.test(n)) return 5;
  if (/\b(bachelors?|undergraduate)\b/.test(n)) return 4;
  return null;
}

/** Every degree level a label names (bachelor's 4, master's 5, doctorate 6). */
function degreeLevelsNamed(n: string): number[] {
  const out: number[] = [];
  if (/\b(bachelors?|undergraduate)\b/.test(n)) out.push(4);
  if (/\b(masters?|mba)\b/.test(n)) out.push(5);
  if (/\b(phd|ph d|doctoral|doctorate)\b/.test(n)) out.push(6);
  return out;
}

/** A degree in the sciences, engineering or mathematics, by its own words. */
const STEM_DEGREE =
  /\b(engineering|engineer|computer|computing|comput\w*|software|science|sciences|scientist|math\w*|physics|statistics|statistical|data|economics|econometrics|informatics|information systems|electrical|mechanical|mechatronics|chemistry|chemical|biology|bioinformatics|technology|basc|bsc|beng|btech|msc|meng)\b/;

/** Is the applicant in a program at that level (or one of those levels) right now? From the rows in progress. */
function programAtLevel(q: QuestionInput, level: number | number[], facts: ProfileFacts, rule: string): QuestionResult {
  const want = Array.isArray(level) ? level : [level];
  const inProgress = facts.education.entries.filter((x) => x.completed === false);
  if (inProgress.length === 0) {
    return isHigh(facts.education.currentlyEnrolled) && facts.education.currentlyEnrolled.value === false
      ? booleanResult(false, q, rule)
      : abstain(rule + ":unknown");
  }
  if (inProgress.some((x) => x.rank === null)) return abstain(rule + ":unknown");
  return booleanResult(inProgress.some((x) => x.rank !== null && want.includes(x.rank)), q, rule);
}

/**
 * "Which degree are you currently pursuing?" (Superhuman on Ashby, live
 * 2026-10-03): the level of the degree in progress, ranked like the options.
 * A graduate pursues none: the list's own "not a student" option, else blank
 * and kept from the AI, which could only pick a degree they are not taking.
 */
function resolvePursuedDegree(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\b(which|what)\b.*\b(degree|program|level)\b.*\b(pursuing|working towards?|enrolled in|studying for)\b/.test(n)) return null;
  // "The highest degree level you are currently pursuing" (CTC, question bank
  // 2026-10-05) is still the one in progress: graduates got their old degree.
  if (/\bor (have |has )?(completed|graduated|earned|obtained)\b|\bmost recent\b/.test(n) || (/\bhighest\b/.test(n) && !/\bcurrently pursuing\b/.test(n))) return null;
  if (isBooleanQuestion(q)) return null;
  const entries = facts.education.entries;
  const inProgress = entries.filter((x) => x.completed === false);
  // Typed in a box ("Please include what degree you are currently pursuing in
  // addition to major(s)", Datacor, question bank 2026-10-05): the degree in
  // progress as the profile names it, major included. A graduate's major was
  // written there.
  if (!q.options?.length) {
    if (inProgress.length === 1 && inProgress[0].degree) return answer(inProgress[0].degree, "pursuing:text");
    if (inProgress.length === 0 && entries.length > 0 && entries.every((x) => x.completed === true)) return abstain("pursuing:not-enrolled");
    return abstain("pursuing:unknown");
  }
  if (inProgress.length === 0) {
    if (entries.length === 0 || entries.some((x) => x.completed !== true)) return abstain("pursuing:unknown");
    const none = q.options.filter((o) => /\bnot (currently )?(pursuing|enrolled|a student)\b|^(none|n a|not applicable)\b/.test(qnorm(o)));
    return none.length === 1 ? answer(none[0], "pursuing:none") : abstain("pursuing:not-enrolled");
  }
  if (inProgress.some((x) => x.rank === null)) return abstain("pursuing:unranked");
  const top = Math.max(...inProgress.map((x) => x.rank as number));
  const hits = q.options.filter((o) => optionRank(o) === top);
  return hits.length === 1 ? answer(hits[0], "pursuing") : abstain("pursuing:no-unique-option");
}

/** "Are you currently an advanced PhD candidate?" (NTT DATA on Ashby, live 2026-10-03). */
function resolveDegreeCandidate(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\bare you (currently )?(a |an )?(advanced )?\w*\s?(phd|ph d|doctoral|doctorate|masters?|mba|undergraduate|bachelors?) (candidate|student)\b/.test(n)) return null;
  if (!isBooleanQuestion(q) && q.controlType !== "combobox") return null;
  const level = degreeLevelAsked(n);
  return level === null ? null : programAtLevel(q, level, facts, "degree-candidate");
}

/** "Are you attending or a recent graduate of the University of X?" */
function resolveSchoolMembership(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  // "…in pursuit of a MS degree in … at Northeastern University?" (Bracebridge
  // on Greenhouse, live, round 8) names its school after the program.
  const m = /\b(?:attend(?:ing|ed)?|graduate(?:d)? (?:of|from)|student (?:at|of)|alum(?:nus|na|ni)? of|studying at|(?:in pursuit of|pursuing|enrolled in|working towards?)\b[^?]{0,100}?\bat)\s+(?:the\s+)?((?:university|college|institute|school|polytechnic)\b[^?,]*|[a-z .&-]+ (?:university|college|institute))/.exec(n);
  if (!m || !isBooleanQuestion(q)) return null;
  const named = qnorm(m[1]).replace(/^the /, "");
  // "…or have graduated from a university?" names no school: an education question.
  if (/^(a|an|any|some|one|your|accredited)\b/.test(named)) return null;
  const entries = facts.education.entries;
  if (entries.length === 0) return abstain("school-membership:no-education");
  const there = entries.filter((e) => {
    const s = qnorm(e.school);
    return s && (s.includes(named) || named.includes(s));
  });
  // Now, or a degree being pursued: only a degree still running there counts.
  // A Northeastern graduate was told Yes to "Are you currently attending
  // Northeastern University?" (round 8).
  const pursuit = /\b(in pursuit of|pursuing|enrolled in|working towards?)\b/.test(n);
  // "…attending or a recent graduate of…" counts its graduates too.
  const graduatesToo = /\b(graduate(d)? (of|from)|alum(nus|na|ni)?|attended|have you (ever )?(attended|studied))\b/.test(n);
  const present = !graduatesToo && (pursuit || /\b(currently|presently|now)\b/.test(n) || /\b(are|do) you\b[^?]{0,15}\b(attend|attending|studying|a student)\b/.test(n));
  if (!present) return booleanResult(there.length > 0, q, "school-membership");
  if (there.every((e) => e.completed === true)) return booleanResult(false, q, "school-membership:not-there-now");
  // Enrolled there: a pursuit names a program the profile may not be in.
  if (pursuit) return abstain("school-membership:program-unknown");
  return there.some((e) => e.completed === false) ? booleanResult(true, q, "school-membership") : abstain("school-membership:status-unknown");
}

function resolveGraduation(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  // "When do you expect to complete your degree?" (Stripe, question bank
  // 2026-10-05) got the degree's name: it asks when it ends.
  const completes = /\bwhen\b[^?]*\b(complete|completing|finish|finishing)\b[^?]*\b(degree|program|programme|studies|education)\b/.test(n);
  if (!completes && (!/\bgraduat(e|ed|ion|ing)\b/.test(n) || !/\b(year|date|when|month|term|semester|expected|anticipated)\b/.test(n))) return null;
  if (q.kind === "boolean") return null;
  if (completes && !q.options?.length && q.kind !== "date") {
    const p = facts.education.primary;
    if (!p?.graduation) return abstain("graduation:unknown");
    if (p.completed === true && /\bexpect|\bwill\b/.test(n)) return abstain("graduation:not-enrolled");
    const g = p.graduation;
    const y = String(g.earliest.getUTCFullYear());
    return answer(g.precision === "year" ? y : `${g.earliest.toLocaleString("en-US", { month: "long", timeZone: "UTC" })} ${y}`, "graduation:completion");
  }
  // "…degree result, or expected result if you have not yet graduated?"
  // (Canonical, question bank 2026-10-05) asks for a grade: resolveGpa's.
  if (/\b(degree|university|academic|final|expected) (result|results|grade|grades|classification)\b/.test(n)) return null;
  // "What year did you / will you graduate from university (undergrad)?" (K1
  // on JazzHR, regression 2026-10-05) got the master's year: the degree of
  // the level asked, when the profile has exactly one.
  const levelAsked = /\b(undergrad|undergraduate|bachelor s|bachelors|bachelor)\b/.test(n) ? 4 : /\b(master s|masters|master|graduate degree|graduate school|grad school)\b/.test(n) ? 5 : null;
  const atLevel = levelAsked === null ? [] : facts.education.entries.filter((e) => e.rank === levelAsked);
  if (levelAsked !== null && atLevel.length === 0 && facts.education.entries.length > 0 && facts.education.entries.every((e) => e.rank !== null)) return abstain("graduation:no-such-degree");
  const primary = atLevel.length === 1 ? atLevel[0] : facts.education.primary;
  const g = primary?.graduation;
  if (!g) return abstain("graduation:unknown");
  const year = String(g.earliest.getUTCFullYear());
  const monthName = g.earliest.toLocaleString("en-US", { month: "long", timeZone: "UTC" });
  if (q.options && q.options.length) {
    // A list of the months asks the month: "What is your expected graduation
    // month?" (Resultant), "…expected (or actual) graduation month?" (Western
    // Digital); SmartRecruiters bank, round 7: blank for an April 2027
    // student. A graduate answers only the "actual" one.
    const monthOf = (o: string): number => {
      const t = qnorm(o);
      const i = MONTH_WORDS.indexOf(t.slice(0, 3));
      return i >= 0 && /^[a-z]+$/.test(t) && (t.length === 3 || t.startsWith(MONTH_WORDS[i])) ? i : -1;
    };
    if (q.options.filter((o) => monthOf(o) >= 0).length >= 10) {
      // A graduate is asked only an actual month; a month the profile does
      // not hold is nobody's to read (bank r7d sent it to the AI).
      if (primary?.completed === true && /\b(expected|anticipated|projected)\b/.test(n) && !/\bactual\b/.test(n)) return abstain("graduation:not-enrolled");
      if (g.precision === "year") return abstain("graduation:month-unknown");
      const month = q.options.filter((o) => monthOf(o) === g.earliest.getUTCMonth());
      return month.length === 1 ? answer(month[0], "graduation:month") : abstain("graduation:no-unique-option");
    }
    // Year-only options take the year; month-bearing options need the month.
    const yearOnly = q.options.filter((o) => new RegExp(`^\\s*${year}\\s*$`).test(o));
    if (yearOnly.length === 1) return answer(yearOnly[0], "graduation:year");
    // A degree finished is "already graduated" where the list says so, before
    // any term: June 2026 went to "August 2026 - December 2026" (DV Trading,
    // question bank 2026-10-05).
    const graduatedFirst = q.options.filter((o) => /\b(already graduated|graduated already|have already graduated)\b/.test(qnorm(o)));
    if (graduatedFirst.length === 1 && primary?.completed === true) return answer(graduatedFirst[0], "graduation:already-graduated");
    if (g.precision !== "year") {
      // The month and year as words, never a number inside a range of years:
      // the fuzzy matcher read "August 2026 - December 2026" as 2026 to 2026.
      const said = qnorm(`${monthName} ${year}`);
      const hits = q.options.filter((o) => ` ${qnorm(o)} `.includes(` ${said} `));
      if (hits.length === 1) return answer(hits[0], "graduation:month-year");
      // "January - June 2027", "December 2026 - November 2027", "Spring 2027":
      // the one option whose months contain the graduation month.
      const at = g.earliest.getUTCFullYear() * 12 + g.earliest.getUTCMonth();
      const containing = q.options.filter((o) => {
        const span = optionMonthSpan(o);
        return span !== null && span[0] <= at && at <= span[1];
      });
      if (containing.length === 1) return answer(containing[0], "graduation:month-range");
    }
    // "Earlier than Fall 2026" / "Later than Summer 2027" (Databricks) and
    // "Already graduated" (Riot; question bank 2026-10-05): open ends,
    // compared with the whole span the graduation could be in.
    const first = g.earliest.getUTCFullYear() * 12 + g.earliest.getUTCMonth();
    const last = g.latest.getUTCFullYear() * 12 + g.latest.getUTCMonth();
    // "Anything before Fall/Winter 2026" (Duolingo on Greenhouse, live) and
    // "Prior to 2023", a bare year (Resultant, SmartRecruiters bank); round 7.
    const open = q.options.filter((o) => {
      const m = /^(?:anything |any time |anytime |sometime )?(earlier than|before|prior to|later than|after)\b/.exec(qnorm(o));
      const bare = /^\D*\b((?:19|20)\d{2})\b\D*$/.exec(o);
      const span = m ? (optionMonthSpan(o) ?? (bare ? ([Number(bare[1]) * 12, Number(bare[1]) * 12 + 11] as [number, number]) : null)) : null;
      if (!m || !span) return false;
      return /^(earlier|before|prior)/.test(m[1]) ? last < span[0] : first > span[1];
    });
    if (open.length === 1) return answer(open[0], "graduation:open-range");
    const graduated = q.options.filter((o) => /\b(already graduated|graduated already|have already graduated)\b/.test(qnorm(o)));
    if (graduated.length === 1 && primary.completed === true) return answer(graduated[0], "graduation:already-graduated");
    // With the month known, an option naming another month or term is not
    // theirs: "December 2026" for a June 2026 graduate (Perchwell, Ashby bank
    // 2026-10-08).
    // A winter term is no month we can place (December, or January to
    // March): "Winter 2027" was the April 2027 graduate's on the year alone
    // (Grow Therapy on Ashby, live 2026-10-08).
    const withYear = q.options.filter((o) => o.includes(year) && (g.precision === "year" || (optionMonthSpan(o) === null && !/\bwinter\b/i.test(o))));
    if (withYear.length === 1) return answer(withYear[0], "graduation:only-option-in-year");
    // A year list without theirs, beside its "Other" (Palantir's 2022-2030
    // for a 2016 graduate, live 2026-10-03): "Other" is the true one.
    const other = q.options.filter((o) => /^\s*other\b/i.test(o));
    if (other.length === 1 && q.options.filter((o) => /^\s*(19|20)\d{2}\s*$/.test(o)).length >= 2) return answer(other[0], "graduation:other-year");
    // An EXPECTED graduation asked of a graduate: there is none to give, and
    // the AI could only invent one (Superhuman on Ashby, live 2026-10-03).
    if (primary.completed === true && /\b(expected|anticipated|projected)\b|\bwhen (will|do) you graduate\b/.test(n)) {
      return abstain("graduation:not-enrolled");
    }
    return abstain("graduation:no-unique-option");
  }
  // "…expected month and year of graduation?" in a text box (NinjaHoldings on
  // Breezy, live 2026-10-03: "2027" for December 2027): the month it asks
  // for, or nothing when the profile knows only the year.
  if (/\bmonths?\b/.test(n) && q.kind !== "date") {
    return g.precision === "year" ? abstain("graduation:month-unknown") : answer(`${monthName} ${year}`, "graduation:month-year");
  }
  if (q.kind === "date" || /\bdate\b/.test(n)) {
    // A month and year asked as numbers: "(MM/YY)" (Bosch, SmartRecruiters
    // bank, round 7) got "April 2027". A format with a day needs one.
    const stated = statedDateFormat(q.placeholder || "") ?? statedDateFormat(q.label || "");
    if (stated === "MM/YY" || stated === "MM/YYYY") {
      return g.precision === "year" ? abstain("graduation:month-unknown") : answer(formatDateFor(g.earliest, q), "graduation:stated-format");
    }
    if (stated && g.precision !== "day") return abstain("graduation:day-unknown");
    if (g.precision === "year") return answer(year, "graduation:year-as-date", "high");
    // A month is not a day: "April 2027" suits a text box and a month control,
    // and a day-precise picker gets nothing (dateControl), never an invented
    // 1st (Ashby's "Pick date..." got 04/01/2027, live 2026-10-03).
    if (g.precision === "month") return answer(`${monthName} ${year}`, "graduation:month-as-date");
    return answer(formatDateFor(g.earliest, q), "graduation:date");
  }
  return answer(year, "graduation:year");
}

const MONTH_WORDS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
/** Academic seasons as months (0-11): graduation in April / May is Spring. */
const SEASON_MONTHS: Record<string, [number, number]> = { spring: [2, 4], summer: [5, 7], fall: [8, 11], autumn: [8, 11] };

/**
 * The absolute months [from, to] (year * 12 + month) an option covers, or null:
 * "December 2027", "May/June 2028", "January - June 2027",
 * "December 2026 - November 2027", "Spring 2027". Winter is left out: it means
 * Dec-Feb in one convention and Jan-Apr in another (Canadian co-op terms).
 */
/**
 * "Are you graduating in Spring 2027?" (CareerPuck, live 2026-10-05): Yes when
 * the graduation falls in that term, No when it falls outside it (a degree
 * finished before it included), blank when the profile's span (a bare year)
 * only overlaps it. "By"/"before" a term asks on or before its end.
 */
function resolveGraduatingInTerm(q: QuestionInput, facts: ProfileFacts): QuestionResult {
  if (!isBooleanQuestion(q)) return null;
  const m = /\b(?:are|will) you (?:be )?graduat\w*\b[^?]*?\b(in|during|by|before)\s+([^?]+)/i.exec(q.label);
  if (!m) return null;
  const span = optionMonthSpan(m[2]);
  if (!span) return null;
  const g = facts.education.primary?.graduation;
  if (!g) return abstain("graduation-term:unknown");
  const first = g.earliest.getUTCFullYear() * 12 + g.earliest.getUTCMonth();
  const last = g.latest.getUTCFullYear() * 12 + g.latest.getUTCMonth();
  if (/^(by|before)$/i.test(m[1])) {
    if (last <= span[1]) return booleanResult(true, q, "graduation-term");
    if (first > span[1]) return booleanResult(false, q, "graduation-term");
    return abstain("graduation-term:unclear");
  }
  if (last < span[0] || first > span[1]) return booleanResult(false, q, "graduation-term");
  if (first >= span[0] && last <= span[1]) return booleanResult(true, q, "graduation-term");
  return abstain("graduation-term:unclear");
}

export function optionMonthSpan(option: string): [number, number] | null {
  const t = qnorm(option);
  const years = (t.match(/\b(19|20)\d{2}\b/g) ?? []).map(Number);
  if (years.length === 0 || years.length > 2) return null;
  const season = /\b(spring|summer|fall|autumn)\b/.exec(t);
  if (season && years.length === 1) {
    const [a, b] = SEASON_MONTHS[season[1]];
    return [years[0] * 12 + a, years[0] * 12 + b];
  }
  const months: number[] = [];
  for (const w of t.split(" ")) {
    const i = MONTH_WORDS.indexOf(w.slice(0, 3));
    if (i >= 0 && /^[a-z]+$/.test(w) && (w.length === 3 || w.startsWith(MONTH_WORDS[i]))) months.push(i);
  }
  if (months.length === 0 || months.length > 2) return null;
  if (years.length === 2) {
    if (months.length !== 2) return null;
    return [years[0] * 12 + months[0], years[1] * 12 + months[1]];
  }
  const y = years[0];
  return [y * 12 + months[0], y * 12 + months[months.length - 1]];
}

// ----- GPA --------------------------------------------------------------------------

/** "GPA", "Cumulative GPA", "Grade point average": the applicant's stated GPA;
 *  a graduate / doctorate GPA they cannot have is that list's "N/A". */
/**
 * "Undergrad Discipline(s)" (DV Trading, question bank 2026-10-05): the major
 * of the degree at the level asked, never the main degree's. A master's in
 * Information Systems was written for a Computer Science bachelor, and a
 * bootcamp certificate's track answered for someone with no bachelor's.
 */
const LAW_DEGREE = /\b(j ?d|juris doctor\w*|ll ?b|ll ?m|bachelor of laws|master of laws|law)\b/;
const MEDICAL_DEGREE = /\b(m ?d|doctor of medicine|medicine|mbbs)\b/;

/**
 * A school asked at one level of study: "Undergraduate School", "Graduate
 * School", "Law School" and its graduation year (Barnes & Thornburg, Ashby
 * bank 2026-10-08) all got the profile's first school, so a bootcamp stood as
 * an undergraduate school and every university as a law school. The school,
 * or year, of the one degree at that level; with none at it, nothing (and
 * nothing from the AI either).
 */
function resolveSchoolAtLevel(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (isBooleanQuestion(q) || (q.options?.length ?? 0) > 0) return null;
  // Only a label that asks for the school itself, or when it was finished:
  // "What was your bachelor's university degree result…?" asks the GPA
  // (Canonical, question bank 2026-10-05).
  const asked = n.replace(/\b(name|attended|optional|required|if applicable)\b/g, " ").replace(/\s+/g, " ").trim();
  const m =
    /(?:^|\b(?:which|what|your|the) )(law|medical|graduate|grad|master s|masters|doctoral|undergraduate|undergrad|bachelor s|bachelors) (school|university|institution|college)(?: (?:graduation )?(year|date))?(?: did you attend)?$/.exec(asked);
  if (!m) return null;
  const level = m[1] === "law" ? "law" : m[1] === "medical" ? "medical" : /^(undergraduate|undergrad|bachelor s|bachelors)$/.test(m[1]) ? "undergraduate" : "graduate";
  const entries = facts.education.entries.filter((e) => {
    if (level === "law") return LAW_DEGREE.test(qnorm(e.degree));
    if (level === "medical") return MEDICAL_DEGREE.test(qnorm(e.degree));
    return level === "graduate" ? (e.rank ?? 0) >= 5 : e.rank === 4;
  });
  if (entries.length === 0) {
    const ranked = facts.education.entries.length > 0 && facts.education.entries.every((e) => e.rank !== null);
    return abstain(ranked || level === "law" || level === "medical" ? "school-level:none" : "school-level:unknown");
  }
  if (entries.length > 1) return abstain("school-level:several");
  const e = entries[0];
  if (m[3]) {
    const year = e.graduation?.earliest.getUTCFullYear();
    return year ? answer(String(year), "school-level:year") : abstain("school-level:year-unknown");
  }
  return e.school ? answer(e.school, "school-level") : abstain("school-level:unknown");
}

function resolveDisciplineAtLevel(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\b(discipline|disciplines|major|majors|field of study|area of study|concentration)\b/.test(n) || isBooleanQuestion(q)) return null;
  const level = /\b(undergrad|undergraduate|bachelor s|bachelors|bachelor)\b/.test(n)
    ? 4
    : /\b(master s|masters|graduate (degree|school|program|studies|major|field|discipline|concentration)|grad school)\b/.test(n)
      ? 5
      : null;
  if (level === null) return null;
  const entries = facts.education.entries.filter((e) => (level === 4 ? e.rank === 4 : (e.rank ?? 0) >= 5));
  if (entries.length === 0) {
    return facts.education.entries.length > 0 && facts.education.entries.every((e) => e.rank !== null)
      ? abstain("discipline:no-degree-at-level")
      : abstain("discipline:unknown");
  }
  if (entries.length > 1) return abstain("discipline:several");
  const field = deriveFieldOfStudy(entries[0].degree);
  return field ? answer(field, "discipline:level") : abstain("discipline:unknown");
}

/** The scale a stated GPA is on: written ("8.6/10", "3.7 out of 4.0"), or 4
 *  for a bare number a 4.0 scale holds ("3.85"); null when unknown. */
function gpaScale(stated: string): number | null {
  const written = /\/\s*(\d+(?:\.\d+)?)|\bout of\s*(\d+(?:\.\d+)?)/i.exec(stated);
  if (written) return Math.round(parseFloat(written[1] ?? written[2]));
  if (/%/.test(stated)) return 100;
  const n = parseFloat(/\d+(\.\d+)?/.exec(stated)?.[0] ?? "");
  return Number.isFinite(n) && n <= 4.3 ? 4 : null;
}

/** The scale a list of GPA options is on, from its largest number (a "4+"
 *  or "3.8 - 4.0" list is out of 4); null when the options carry none. */
function optionsGpaScale(options: string[] | undefined): number | null {
  const nums = (options ?? []).flatMap((o) => (o.match(/\d+(\.\d+)?/g) ?? []).map(Number)).filter((x) => Number.isFinite(x));
  if (nums.length === 0) return null;
  const top = Math.max(...nums);
  return top <= 4.5 ? 4 : top <= 5.5 ? 5 : top <= 10 ? 10 : null;
}

function resolveGpa(q: QuestionInput, n: string, profile: UserApplicationProfile, facts: ProfileFacts): QuestionResult {
  // "What was your bachelor's university degree result…? Please include the
  // grading system" (Canonical, question bank 2026-10-05) is the GPA too.
  const result = /\b(degree|university|academic|final) (result|results|grade|grades|classification)\b|\bexpected result\b/.test(n);
  // ("G.P.A." reads "g p a", SRC on Jobvite, question bank 2026-10-17.)
  if (!/\b(c ?)?g ?p ?a\b|\bgrade point average\b|\bcumulative average\b/.test(n) && !result) return null;
  if (/\b(scale|out of|maximum|max)\b/.test(n) && !/\byour\b/.test(n)) return null;
  const higher = /\b(graduate|masters?|doctorate|doctoral|phd|mba)\b/.test(n) && !/\bundergraduate\b/.test(n);
  const rank = facts.education.highestRank?.value ?? null;
  if (higher && rank !== null && rank < 5) {
    const na = (q.options ?? []).filter((o) => /^(n a|na|none|i do not have|no graduate)\b|\bnot applicable\b/.test(qnorm(o)));
    return na.length === 1 ? answer(na[0], "gpa:not-applicable") : abstain("gpa:no-graduate-degree");
  }
  // The profile holds one GPA, the main degree's: an undergraduate GPA asked
  // of someone whose main degree is a master's is not it (Duolingo, question
  // bank 2026-10-05: an OPT holder's master's 3.85 as her undergraduate GPA).
  const primaryRank = facts.education.primary ? degreeRank(facts.education.primary.degree) : null;
  if (/\b(undergraduate|undergrad|bachelor s|bachelors|bachelor|first degree)\b/.test(n) && primaryRank !== null && primaryRank >= 5) return abstain("gpa:graduate-degree-gpa");
  const stated = (profile.gpa ?? "").trim();
  if (!stated) return abstain("gpa:unknown");
  const num = /\d+(\.\d+)?/.exec(stated)?.[0] ?? null;
  // A GPA on one scale is no number on another: 8.6 of 10 went into "3.75+"
  // and "4+" of 4.0-scale lists (DoorDash, Klaviyo; question bank 2026-10-05).
  const statedScale = gpaScale(stated);
  const askedScale = /\b(4(\.0+)?|four)( point)? scale\b|\bout of 4(\.0+)?\b/.test(n)
    ? 4
    : /\b10(\.0+)?( point)? scale\b|\bout of 10\b/.test(n)
      ? 10
      : optionsGpaScale(q.options);
  if (askedScale !== null && statedScale !== askedScale) return abstain("gpa:other-scale");
  if (q.options && q.options.length) {
    const hit = (num && pickOption(q.options, num)) || pickOption(q.options, stated);
    return hit ? answer(hit, "gpa:option") : abstain("gpa:no-matching-option");
  }
  if (q.kind === "number") return num ? answer(num, "gpa:number") : abstain("gpa:not-numeric");
  return answer(stated, "gpa:stated");
}

/**
 * "If selected for the internship, what would be your preferred start date?"
 * offered as dates ("May 3, 2027" | "May 17, 2027" | "June 1, 2027", The
 * Exploration Company on Ashby, live 2026-10-03): the applicant's earliest
 * start when it is one of them, else the first date on or after it.
 */
function resolveStartDateChoice(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\b(start|starting|join|joining|availability|available)\b[^?]*\bdate\b|\bwhen (can|could|would) you (start|begin|join)\b/.test(n)) return null;
  if (!q.options || q.options.length < 2) return null;
  const av = facts.availability.earliestStart;
  const dated = q.options
    .map((o) => ({ o, d: /\d{4}/.test(o) ? new Date(o.replace(/(\d)(st|nd|rd|th)\b/, "$1") + " UTC") : null }))
    .filter((x): x is { o: string; d: Date } => x.d !== null && !Number.isNaN(x.d.getTime()));
  if (dated.length < 2 || dated.length !== q.options.filter((o) => o.trim()).length) return null;
  if (!isHigh(av)) return abstain("start-date-choice:unknown");
  const day = (d: Date): number => Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const target = day(av.value);
  const onOrAfter = dated.filter((x) => day(x.d) >= target).sort((a, b) => day(a.d) - day(b.d));
  return onOrAfter.length > 0 ? answer(onOrAfter[0].o, "start-date-choice") : abstain("start-date-choice:all-earlier");
}

/** An option's span in days from now ("Immediately", "2 to 4 weeks from offer
 *  acceptance", "12+ weeks", "Over a month from offer"), or null. */
function durationRange(option: string): [number, number] | null {
  const WORDS: Record<string, number> = { a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, eight: 8, ten: 10, twelve: 12 };
  const t = qnorm(option).replace(/\b(an?|one|two|three|four|five|six|eight|ten|twelve)\b(?= (day|week|month)s?\b)/g, (w) => String(WORDS[w]));
  if (/\b(immediately|immediate|asap|right away)\b/.test(t)) return [0, 7];
  // "With Reasonable Notice" beside "Immediately" (Ross Video on Jobvite,
  // question bank 2026-10-17): a notice period, up to three months' notice.
  if (/\b(with|after|upon) (a |the )?(reasonable |standard |proper |appropriate |usual |sufficient |giving |serving |my )*notice\b/.test(t)) return [8, 120];
  const unit = (u: string): number => (u.startsWith("day") ? 1 : u.startsWith("week") ? 7 : 30);
  let m: RegExpExecArray | null;
  if ((m = /\b(\d+)\s*(?:to\s+|\s)(\d+)\s*(day|week|month)s?\b/.exec(t))) return [+m[1] * unit(m[3]), +m[2] * unit(m[3])];
  if ((m = /\b(\d+)\s*\+\s*(day|week|month)s?\b/.exec(t))) return [+m[1] * unit(m[2]), Infinity];
  // "8 weeks +", "8 weeks or more" (Exegy, Ashby bank 2026-10-08): read as
  // "about 8 weeks", it took a start 53 days away from "4 - 8 weeks".
  if ((m = /\b(\d+)\s*(day|week|month)s?\s*(\+|or (more|longer)|and (more|above|up))/.exec(t))) return [+m[1] * unit(m[2]), Infinity];
  if ((m = /\b(more than|over|longer than|after|beyond)\s+(\d+)\s*(day|week|month)s?\b/.exec(t))) return [+m[2] * unit(m[3]) + 1, Infinity];
  if ((m = /\b(within|less than|under|up to|no more than)\s+(\d+)\s*(day|week|month)s?\b/.exec(t))) return [0, +m[2] * unit(m[3])];
  if ((m = /\b(\d+)\s*(day|week|month)s?\b/.exec(t))) {
    const d = +m[1] * unit(m[2]);
    return [Math.max(0, d - 3), d + 3];
  }
  return null;
}

/**
 * "Availability?" / "earliest available start date" answered with spans from
 * now ("Immediately" | "Two weeks from offer" | "Over a month from offer",
 * Agiloft on Lever; "2 to 4 weeks … 12+ weeks from offer acceptance",
 * Striveworks): the span holding the days until the applicant's earliest start.
 */
function resolveStartBucket(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!q.options || q.options.length < 2) return null;
  // A length is no start: "What is the available term length for your
  // upcoming co-op work term?" (GDMSI, SmartRecruiters bank, round 7) got
  // "4 months" for applicants with no co-op at all.
  if (/\b(term length|length of|how long|duration|how many (months|weeks))\b/.test(n)) return null;
  // A notice list too, by days: "If you receive an offer, how much notice
  // would you need…?" (FiscalNote on Lever, question bank 2026-10-08) matched
  // its options by text, "3 months" took "3-4 weeks".
  if (!/\b(availability|available|start|starting|join|joining|begin|notice)\b/.test(n)) return null;
  const spans = q.options.map((o) => ({ o, r: durationRange(o) })).filter((x): x is { o: string; r: [number, number] } => x.r !== null);
  if (spans.length < 2) return null;
  // "What is your notice period to begin working with Exegy?" (Ashby bank
  // 2026-10-08): the notice stated, before the days until a start date.
  const notice = /\bnotice\b/.test(n) ? facts.availability.noticeDays : null;
  const av = facts.availability.earliestStart;
  if (!(notice && isHigh(notice)) && !isHigh(av)) return abstain("start-bucket:unknown");
  const days = notice && isHigh(notice) ? Math.max(0, notice.value) : Math.max(0, Math.ceil((av!.value.getTime() - facts.today.getTime()) / 86400000));
  const hits = spans.filter((x) => days >= x.r[0] && days <= x.r[1]).sort((a, b) => a.r[1] - a.r[0] - (b.r[1] - b.r[0]));
  if (hits.length > 0) return answer(hits[0].o, "start-bucket");
  // Between two ranges (11 days: past "Immediately", short of "2 to 4
  // weeks"; Striveworks, question bank 2026-10-08): the next later range is
  // still true, an earlier one would promise a start they cannot make.
  const later = spans.filter((x) => x.r[0] > days).sort((a, b) => a.r[0] - b.r[0]);
  return later.length > 0 ? answer(later[0].o, "start-bucket:next-later") : abstain("start-bucket:no-matching-option");
}

/**
 * A start asked as a year or a quarter: "In which year would you be available
 * to start at Galatea?" [2026 | … | 2030] and "In which quarter would you be
 * available to start?" [January - March (Q1) | …] (Galatea Associates on
 * Lever, live, round 7: blank). The stated earliest start's year, its months.
 */
function resolveStartCalendar(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  const opts = (q.options ?? []).filter((o) => o.trim());
  if (opts.length < 2) return null;
  if (!/\b(availab\w*|start|starting|join|joining|begin|commence)\b/.test(n) || /\b(graduat\w*|end|ending|finish\w*|until)\b/.test(n)) return null;
  const yearOf = (o: string): number | null => {
    const m = /^\s*((?:19|20)\d{2})\s*$/.exec(o);
    return m ? Number(m[1]) : null;
  };
  const monthsOf = (o: string): number[] => {
    const out: number[] = [];
    for (const w of qnorm(o).split(" ")) {
      const i = MONTH_WORDS.indexOf(w.slice(0, 3));
      if (i >= 0 && /^[a-z]+$/.test(w) && (w.length === 3 || w.startsWith(MONTH_WORDS[i]))) out.push(i);
    }
    return out;
  };
  const yearList = opts.every((o) => yearOf(o) !== null);
  const monthList = !yearList && opts.every((o) => monthsOf(o).length === 2 && !/\b(19|20)\d{2}\b/.test(o));
  if (!yearList && !monthList) return null;
  const av = facts.availability.earliestStart;
  if (!isHigh(av)) return abstain("start-calendar:unknown");
  const at = new Date(Math.max(av.value.getTime(), facts.today.getTime()));
  const hits = yearList
    ? opts.filter((o) => yearOf(o) === at.getUTCFullYear())
    : opts.filter((o) => {
        const [a, b] = monthsOf(o);
        return a <= at.getUTCMonth() && at.getUTCMonth() <= b;
      });
  return hits.length === 1 ? answer(hits[0], "start-calendar") : abstain("start-calendar:no-matching-option");
}

/** Time zones by region; a state split between zones is left out (not guessed). */
const ZONE_OF_REGION: Record<string, string> = {
  // Canada
  "CA-BC": "pacific", "CA-AB": "mountain", "CA-SK": "central", "CA-MB": "central", "CA-ON": "eastern", "CA-QC": "eastern",
  "CA-NB": "atlantic", "CA-NS": "atlantic", "CA-PE": "atlantic", "CA-NL": "newfoundland", "CA-NT": "mountain",
  // United States (whole states only)
  "US-NY": "eastern", "US-NJ": "eastern", "US-PA": "eastern", "US-MA": "eastern", "US-CT": "eastern", "US-RI": "eastern",
  "US-VT": "eastern", "US-NH": "eastern", "US-ME": "eastern", "US-DE": "eastern", "US-MD": "eastern", "US-DC": "eastern",
  "US-VA": "eastern", "US-WV": "eastern", "US-NC": "eastern", "US-SC": "eastern", "US-GA": "eastern", "US-OH": "eastern",
  "US-IL": "central", "US-WI": "central", "US-MN": "central", "US-IA": "central", "US-MO": "central", "US-AR": "central",
  "US-LA": "central", "US-MS": "central", "US-AL": "central", "US-OK": "central",
  "US-CO": "mountain", "US-UT": "mountain", "US-WY": "mountain", "US-MT": "mountain", "US-NM": "mountain", "US-AZ": "mountain",
  "US-CA": "pacific", "US-WA": "pacific", "US-NV": "pacific",
};
const ZONE_OPTION: Record<string, RegExp> = {
  pacific: /\b(p[sd]?t|pacific)\b/, mountain: /\b(m[sd]?t|mountain)\b/, central: /\b(c[sd]?t|central)\b/,
  eastern: /\b(e[sd]?t|eastern)\b/, atlantic: /\b(a[sd]?t|atlantic)\b/, newfoundland: /\b(n[sd]?t|newfoundland)\b/,
};
/** "Which timezone are you currently located in?" [PST|MST|CST|EST] (Veeva on Lever). */
function resolveTimezone(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\btime ?zone\b/.test(n) || !q.options?.length) return null;
  if (/\b(prefer|preferred|willing|able to work|overlap|collaborat)\w*\b/.test(n)) return null; // a preference, not where they are
  const region = isHigh(facts.location.region) ? facts.location.region.value : null;
  const zone = region ? ZONE_OF_REGION[region.country + "-" + region.code] : undefined;
  if (!zone) return abstain("timezone:unknown");
  const hits = q.options.filter((o) => ZONE_OPTION[zone].test(qnorm(o)));
  return hits.length === 1 ? answer(hits[0], "timezone") : abstain("timezone:no-matching-option");
}

/** Currencies an option list names, by the words a form uses. */
const CURRENCY_OPTION: Record<string, RegExp> = {
  USD: /\b(us|u s|united states) dollars?\b|\busd\b/,
  CAD: /\bcanadian dollars?\b|\bcad\b/,
  GBP: /\b(british )?pounds? sterling\b|\bbritish pounds?\b|\bgbp\b/,
  EUR: /\beuros?\b|\beur\b/,
  AUD: /\baustralian dollars?\b|\baud\b/,
  INR: /\bindian rupees?\b|\binr\b/,
  JPY: /\bjapanese yen\b|\bjpy\b/,
};
const DOLLAR_BY_COUNTRY: Record<string, string> = ownKeys({ US: "USD", CA: "CAD", AU: "AUD", NZ: "NZD", SG: "SGD", HK: "HKD" });

/** A country's currency, for the countries salaries are asked in most. */
const CURRENCY_BY_COUNTRY: Record<string, string> = ownKeys({
  ...DOLLAR_BY_COUNTRY, GB: "GBP", IN: "INR", JP: "JPY",
  DE: "EUR", FR: "EUR", NL: "EUR", IE: "EUR", ES: "EUR", IT: "EUR", PT: "EUR", BE: "EUR", AT: "EUR", FI: "EUR",
});

/** The currency a salary is written in: its symbol or code, "$" by the
 *  applicant's country; null for a bare number. */
function statedCurrency(salary: string, home: string | null): string | null {
  const s = salary.toLowerCase();
  return /£|\bgbp\b|\bpounds?\b/.test(s) ? "GBP"
    : /€|\beur\b|\beuros?\b/.test(s) ? "EUR"
      : /₹|\binr\b|\brupees?\b/.test(s) ? "INR"
        : /¥|￥|\bjpy\b|\byen\b/.test(s) ? "JPY"
          : /\bcad\b|\bc\$|\bca\$/.test(s) ? "CAD"
            : /\busd\b|\bus\$/.test(s) ? "USD"
              : /\$/.test(s) && home ? DOLLAR_BY_COUNTRY[home] ?? null
                // A bare figure is in the applicant's own currency: a Denver
                // resident's "85000" went into Float's "salary (in CAD)" box
                // (Ashby bank 2026-10-08).
                : /^[\d\s,.]+$/.test(s) && home ? CURRENCY_BY_COUNTRY[home] ?? null
                  : null;
}

/** The currency a question names ("desired salary (CAD$)"), or null. */
function namedCurrency(text: string): string | null {
  const t = text.toLowerCase();
  return /\bcad\b|\bc\$|\bca\$|canadian dollars?/.test(t) ? "CAD"
    : /\busd\b|\bus\$|\bus dollars?/.test(t) ? "USD"
      : /€|\beur\b|\beuros?\b/.test(t) ? "EUR"
        : /£|\bgbp\b|\bpounds?\b/.test(t) ? "GBP"
          : /₹|\binr\b|\brupees?\b/.test(t) ? "INR"
            : null;
}

/**
 * A salary in another currency than the one asked is no answer: €120.000
 * went into "$120,000" and "111-120k" of US lists (Clearway, NISC), US
 * dollars into "desired salary (CAD$)" (A Thinking Ape); question bank
 * 2026-10-05. A list's currency is its label's, its own symbols' or the job
 * country's; a text box is guarded only when its label names one (a stated
 * "€120.000" typed into a plain box still says what it is).
 */
function resolveSalaryCurrency(q: QuestionInput, n: string, facts: ProfileFacts, profile: UserApplicationProfile, ctx: QuestionContext): QuestionResult {
  if (q.category !== "salary" && !/\b(salary|salaries|compensation|pay|wage|wages)\b/.test(n)) return null;
  const salary = (profile.salaryExpectation || "").trim();
  if (!salary) return null;
  const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
  const stated = statedCurrency(salary, home);
  if (!stated) return null;
  const opts = (q.options ?? []).filter((o) => /\d/.test(o));
  const asked = opts.length >= 2
    ? namedCurrency(q.label) ??
      namedCurrency(opts.join(" ")) ??
      (opts.some((o) => o.includes("$")) ? DOLLAR_BY_COUNTRY[ctx.jobCountry ?? "US"] ?? "USD" : null) ??
      (ctx.jobCountry ? CURRENCY_BY_COUNTRY[ctx.jobCountry] ?? null : null)
    : namedCurrency(q.label);
  return asked && asked !== stated ? abstain("salary:other-currency") : null;
}

/**
 * A salary's currency or pay period asked as its own select beside the amount
 * (Breezy: "US Dollar ($)" … and "Hourly | Weekly | Monthly | Yearly", left
 * blank on every live page, 2026-10-03). Both read off the stated salary: the
 * symbol or code ("£", "€", "CAD"), "$" by the applicant's country; "/hour"
 * is Hourly, an annual-sized figure Yearly.
 */
function resolveSalaryUnit(q: QuestionInput, n: string, facts: ProfileFacts, profile: UserApplicationProfile): QuestionResult {
  const opts = (q.options ?? []).filter((o) => o.trim());
  if (opts.length < 2 || isBooleanQuestion(q)) return null;
  if (q.category !== "salary" && !/\b(salary|compensation|pay|wage|rate)\b/.test(n)) return null;
  const salary = (profile.salaryExpectation || "").trim();
  const s = salary.toLowerCase();
  const isCurrencyList = opts.filter((o) => /\b(dollar|euro|pound|rupee|yen|franc|peso|yuan|krona|krone|rand|real)s?\b/i.test(o)).length >= 3;
  if (isCurrencyList) {
    if (!salary) return abstain("salary-currency:unknown");
    const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
    const code = /£|\bgbp\b|\bpounds?\b/.test(s) ? "GBP"
      : /€|\beur\b|\beuros?\b/.test(s) ? "EUR"
      : /₹|\binr\b|\brupees?\b/.test(s) ? "INR"
      : /¥|￥|\bjpy\b|\byen\b/.test(s) ? "JPY"
      : /\bcad\b|\bc\$/.test(s) ? "CAD"
      : /\busd\b|\bus\$/.test(s) ? "USD"
      : /\$/.test(s) && home ? DOLLAR_BY_COUNTRY[home] ?? null
      : null;
    const re = code ? CURRENCY_OPTION[code] : undefined;
    const hits = re ? opts.filter((o) => re.test(qnorm(o))) : [];
    return hits.length === 1 ? answer(hits[0], "salary-currency") : abstain("salary-currency:unknown");
  }
  const periods = opts.filter((o) => /^(hourly|daily|weekly|bi ?weekly|monthly|yearly|annually|annual|per (hour|day|week|month|year|annum))$/.test(qnorm(o)));
  if (periods.length >= 2) {
    if (!salary) return abstain("salary-period:unknown");
    const per = /\/\s*(h|hr|hour)\b|\b(hourly|per hour|an hour)\b/.test(s) ? /\b(hour|hourly)\b/
      : /\/\s*(wk|week)\b|\b(weekly|per week)\b/.test(s) ? /\b(week|weekly)\b/
      : /\/\s*(mo|month)\b|\b(monthly|per month)\b/.test(s) ? /\b(month|monthly)\b/
      : /\/\s*(yr|year)\b|\b(yearly|annual|annually|per year|per annum|a year)\b/.test(s) || Number((s.match(/\d[\d\s,.]*/)?.[0] ?? "").replace(/[\s,]/g, "")) >= 1000
        ? /\b(year|yearly|annual|annually|annum)\b/
        : null;
    const hits = per ? opts.filter((o) => per.test(qnorm(o))) : [];
    return hits.length === 1 ? answer(hits[0], "salary-period") : abstain("salary-period:unknown");
  }
  return null;
}

const MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
/** Academic terms by the month they start ("next Spring (January 2027 …)"). */
const TERM_START: Record<string, number> = { winter: 0, spring: 0, summer: 4, fall: 8, autumn: 8 };

/**
 * Availability for a period the question dates: "Are you available for a
 * full-time onsite internship next Spring (January 2027 - April/May 2027)?"
 * got Yes from the requirement default for an applicant who cannot start
 * before late May (Zipline's embedded Greenhouse form, live 2026-10-03). An
 * earliest start more than a month after the period begins is No; otherwise
 * the other rules (on-site, relocation) decide.
 */
function resolvePeriodAvailability(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\bavailab(le|ility)\b/.test(n) || !isBooleanQuestion(q)) return null;
  const month = new RegExp(`\\b(${MONTH_NAMES.join("|")})\\s+(\\d{4})\\b`).exec(n);
  const term = month ? null : /\b(winter|spring|summer|fall|autumn)\s+(?:term\s+|semester\s+)?(\d{4})\b/.exec(n);
  const start = month
    ? Date.UTC(Number(month[2]), MONTH_NAMES.indexOf(month[1]), 1)
    : term
      ? Date.UTC(Number(term[2]), TERM_START[term[1]], 1)
      : null;
  if (start === null) return null;
  const earliest = facts.availability.earliestStart;
  if (!isHigh(earliest)) return null;
  return earliest.value.getTime() > start + 31 * 86400000 ? booleanResult(false, q, "period-availability:starts-later") : null;
}

/** "Did you Graduate?" (Paylocity's education row, live 2026-10-03): whether
 *  the education the profile means (in progress first) is finished. */
function resolveDidGraduate(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/^(did|have) you graduate(d)?\b/.test(n) || !isBooleanQuestion(q)) return null;
  const done = facts.education.primary?.completed;
  return done === true || done === false ? booleanResult(done, q, "education:graduated") : abstain("education:graduated-unknown");
}

/** A zone named in running text: full names and three-letter codes only ("at"
 *  and "et" are words). */
const ZONE_IN_TEXT: Record<string, RegExp> = {
  pacific: /\b(pacific|p[sd]t)\b/, mountain: /\b(mountain|m[sd]t)\b/, central: /\b(central|c[sd]t)\b/,
  eastern: /\b(eastern|e[sd]t)\b/, atlantic: /\b(atlantic|a[sd]t)\b/, newfoundland: /\b(newfoundland|n[sd]t)\b/,
};

/**
 * "This role requires regular collaboration during Eastern or Pacific Time
 * business hours. Are you available to work within these time zones?" (Voldex
 * on Ashby, left blank 2026-10-03): Yes for an applicant who lives in one of
 * the zones named. Anyone else's answer is a willingness, theirs to give.
 */
function resolveZoneAvailability(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\btime ?zones?\b|\btime business hours\b/.test(n)) return null;
  if (!/\b(available|able) to work\b|\bwork (within|during|in) (these|those|this|the following|the|our)\b/.test(n)) return null;
  if (q.options?.length && !isBooleanQuestion(q)) return null; // "which zone": resolveTimezone
  const named = Object.keys(ZONE_IN_TEXT).filter((z) => ZONE_IN_TEXT[z].test(n));
  if (named.length === 0) return null;
  const region = isHigh(facts.location.region) ? facts.location.region.value : null;
  const zone = region ? ZONE_OF_REGION[region.country + "-" + region.code] : undefined;
  if (!zone) return abstain("timezone:unknown");
  return named.includes(zone) ? booleanResult(true, q, "timezone:lives-in-zone") : abstain("timezone:willingness");
}

/**
 * "This internship will be held in our Chicago office in a hybrid model. Will
 * you be local to Chicago for the summer of 2027?" (Enova on Greenhouse, left
 * blank 2026-10-03): yes for an applicant who lives there or will relocate,
 * no for one who lives elsewhere and will not.
 */
function resolveLocalTo(q: QuestionInput, n: string, facts: ProfileFacts, profile: UserApplicationProfile): QuestionResult {
  // "Are you local to Colorado?" (now): where the applicant lives today, by
  // state, province or country as well as city. "If you are not local to
  // Colorado, are you willing to relocate?" stayed blank for a Seattle
  // applicant who will not move (Anduril on Greenhouse, live 2026-10-05).
  // "Are you CURRENTLY living in/around the Salt Lake City Metro area?"
  // (Cricut, SmartRecruiters bank, round 7) too: blank for six applicants
  // living in other metros or countries.
  const now = /^(?:are you|you are) (?:currently )?(?:local|located|based|living|residing) (?:in or around|in or near|near or in|in around|within|around|near|to|in) (?:the )?(?:greater )?([a-z ]+?)(?: (?:metro|metropolitan))?(?: area| region)?$/.exec(n);
  // "Are you local to or willing to relocate?" (Shield AI on Lever) names no
  // place: the relocation rule's.
  if (now && isBooleanQuestion(q) && !/\b(or|relocat\w*|move|willing)\b/.test(now[1])) {
    const named = now[1].trim();
    const region = regionFromText(named);
    const country = region ? null : countryFromName(named);
    const home = facts.location;
    // Another country is not local to any of this one's states ("If you are
    // not local to Colorado…" from Berlin; question bank 2, 2026-10-05).
    if (region && isHigh(home.country) && home.country.value.code !== region.country) return booleanResult(false, q, "local:other-country");
    if (region && isHigh(home.region)) {
      return booleanResult(home.region.value.code === region.code && home.region.value.country === region.country, q, "local:region");
    }
    if (country && isHigh(home.country)) return booleanResult(home.country.value.code === country.code, q, "local:country");
    const city = isHigh(home.city) ? qnorm(home.city.value) : null;
    if (city && named === city) return booleanResult(true, q, "local:lives-there");
    // A city this code knows: its metro, or its country, against the home's.
    const hintRegion = regionHintForCity(named);
    const cityCountry = countryHintForCity(named) ?? (hintRegion ? (regionFromText(hintRegion)?.country ?? null) : null);
    if (cityCountry && isHigh(home.country) && home.country.value.code !== cityCountry) return booleanResult(false, q, "local:other-country");
    const metro = city ? sameMetro(city, named) : null;
    if (metro !== null) return booleanResult(metro, q, "local:metro");
    return abstain("local:unknown");
  }
  const m = /\bwill you (?:be|live|reside) (?:local|located|living|based) (?:to|in|near) (?:the )?([a-z ]+?)(?: area| region| office)?(?: for| during| this| next| by| in|$)/.exec(n);
  if (!m || !isBooleanQuestion(q)) return null;
  const named = m[1].trim();
  const city = isHigh(facts.location.city) ? qnorm(facts.location.city.value) : null;
  if (city && named === city) return booleanResult(true, q, "local:lives-there");
  // Countries named ("based in the U.S. or Canada", Warp; question bank
  // 2026-10-05): living in one of them is being based there. A Seattle
  // applicant who will not move was answered No.
  const countries = countriesNamedIn(q.label);
  const homeCountry = isHigh(facts.location.country) ? facts.location.country.value.code : null;
  if (countries.length > 0 && homeCountry && countries.includes(homeCountry)) return booleanResult(true, q, "local:country");
  const relocate = polarityOf(profile.willingToRelocate || "");
  if (relocate === true) return booleanResult(true, q, "local:will-relocate");
  if (relocate === false && city) return booleanResult(false, q, "local:lives-elsewhere");
  return abstain("local:unknown");
}

/**
 * "What is the address from which you plan on working? If you would need to
 * relocate, please type "relocating"." (Anthropic on Greenhouse, a real
 * profile, 2026-10-03: the home address was typed for an in-office role in
 * another country). The page's word for an applicant who must move and will;
 * the address (category answer) only when the job is in their own city.
 * Another city at home may be a commute: the applicant's call.
 */
function resolveRelocationInstruction(q: QuestionInput, raw: string, facts: ProfileFacts, profile: UserApplicationProfile, ctx: QuestionContext): QuestionResult {
  if (q.kind !== "text" && q.kind !== "longText") return null;
  const m = /\bif you (?:would |will )?(?:need|plan|intend|have) to (?:relocate|move)\b[^"“'‘]{0,40}["“'‘]([^"”'’]{2,30})["”'’]/i.exec(raw);
  if (!m) return null;
  // Not moving: they work from where they live, wherever the job is (a
  // veteran in Austin who will not relocate, Anthropic, live 2026-10-03).
  if (polarityOf(profile.willingToRelocate || "") === false) return null;
  const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
  if (!ctx.jobCountry || !home) return abstain("relocate-instruction:unknown");
  if (ctx.jobCountry === home) {
    const city = isHigh(facts.location.city) ? qnorm(facts.location.city.value) : null;
    return city && ctx.jobCity && qnorm(ctx.jobCity) === city ? null : abstain("relocate-instruction:unknown");
  }
  return polarityOf(profile.willingToRelocate || "") === true ? answer(m[1].trim(), "relocate-instruction:moving") : abstain("relocate-instruction:unknown");
}

/**
 * "Please indicate your school, program/faculty, and expected month/year of
 * graduation" in a text box (Arc'teryx on Lever, live 2026-10-03): it got
 * "2027". The three facts together, from the education row in progress (or
 * the most recent one).
 */
function resolveEducationSummary(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (q.kind !== "text" && q.kind !== "longText") return null;
  // A degree's RESULT is a grade, not the school and degree (Canonical,
  // question bank 2026-10-05): resolveGpa's.
  if (/\b(result|results|grade|grades|classification|gpa)\b/.test(n)) return null;
  const asksSchool = /\b(school|university|college|institution)\b/.test(n);
  const asksProgram = /\b(program|programme|faculty|degree|major|field of study|discipline)\b/.test(n);
  // The graduation DATE, not the verb: "What school are you currently attending
  // / did you graduate from?" (ZipRecruiter) asks for the school alone.
  const asksGrad =
    /\b(graduation|completion) (date|year|month|term)\b|\b(month|year|date|term)s?( \w+)? of (graduation|completion)\b|\bexpected (month|year|date|term)\b|\bwhen (will|do|would) you graduate\b/.test(n);
  if ([asksSchool, asksProgram, asksGrad].filter(Boolean).length < 2) return null;
  // "Enter your undergraduate major … and the educational institution(s)
  // attended" (Open Data Jobs on Workable, 2026-10-05) got the master's: the
  // bachelor's alone answers it.
  const bachelors = facts.education.entries.filter((x) => x.rank === 4);
  const e = /\bundergrad/.test(n) ? (bachelors.length === 1 ? bachelors[0] : null) : facts.education.primary;
  if (!e?.school) return abstain("education-summary:unknown");
  const parts: string[] = [];
  if (asksSchool) parts.push(e.school);
  if (asksProgram && e.degree) parts.push(e.degree);
  if (asksGrad && e.graduation) {
    const g = e.graduation;
    const y = g.earliest.getUTCFullYear();
    const when = g.precision === "year" ? String(y) : g.earliest.toLocaleString("en-US", { month: "long", timeZone: "UTC" }) + " " + y;
    parts.push((e.completed === false ? "expected graduation " : "graduated ") + when);
  }
  return parts.length >= 2 ? answer(parts.join(", "), "education-summary") : abstain("education-summary:partial");
}

/**
 * "SAT Score*" / "ACT" / "GRE" (SpaceX, live 2026-10-03): the profile holds no
 * test scores. When the list offers a "did not take / do not recall" option,
 * that is the answer that claims nothing; otherwise blank.
 */
/** A test by name; "Act" after a law's words is the law ("Americans with
 *  Disabilities Act" with "may result from" read as an ACT score, and SSCI's
 *  certification went unaccepted; Workable bank, 2026-10-05). */
const TEST_NAME =
  /\b(sat|gre|gmat|lsat|mcat|toefl|ielts|psat)\b|(?<!\b(disabilit(y|ies)|rights|care|protection|privacy|reform|labor|standards|employment|security|accountability|portability|reinvestment|leave|discrimination|opportunity|credit reporting|reporting) )\bact\b/;

function resolveTestScore(q: QuestionInput, n: string): QuestionResult {
  if (!TEST_NAME.test(n) || !/\b(score|scores|test|tests|exam|exams)\b|\b(test|exam|score) results?\b/.test(n)) return null;
  if (!q.options?.length) return q.kind === "text" || q.kind === "number" ? abstain("test-score:unknown") : null;
  const na = q.options.filter((o) => /\b(did not take|have not taken|havent taken|not taken|do not recall|dont recall|not applicable|n a|none)\b/.test(qnorm(o)));
  // "Did not take/Do not recall" beside "Other - did not take" (SpaceX's GRE,
  // left blank 2026-10-03): the plain one.
  const plain = na.length > 1 ? na.filter((o) => !/\bother\b/.test(qnorm(o))) : na;
  return plain.length === 1 ? answer(plain[0], "test-score:none-stated") : abstain("test-score:unknown");
}

/**
 * "Language Skill(s) (Check all that apply)" (Palantir on Lever, live
 * 2026-10-03: "English (ENG)", "French (FRA)", …): the profile's languages
 * among the options, any level but a beginner's.
 */
/** A language's name, the whole option or its first word ("Tamil (India)"). */
const LANGUAGE_NAME =
  /^(english|french|spanish|german|mandarin|chinese|cantonese|portuguese|italian|japanese|korean|hindi|arabic|punjabi|tagalog|filipino|vietnamese|russian|dutch|polish|turkish|hebrew|greek|swedish|norwegian|danish|finnish|czech|hungarian|romanian|ukrainian|bengali|urdu|tamil|telugu|kannada|malayalam|marathi|gujarati|persian|farsi|thai|indonesian|malay|swahili|haitian|irish|welsh|catalan|croatian|serbian|slovak|bulgarian)\b/;

function resolveLanguageChoice(q: QuestionInput, n: string, profile: UserApplicationProfile, facts: ProfileFacts): QuestionResult {
  if (!q.options?.length) return null;
  // A list of languages under a proficiency question names none: "Do you have
  // professional working proficiency in:" [English | French] (Behaviour
  // Interactive on Lever, live, round 7) was blank.
  const named = q.options.filter((o) => LANGUAGE_NAME.test(qnorm(o))).length;
  if (!/\blanguages?\b/.test(n) && !(named >= 2 && /\b(proficien\w*|fluen\w*|speak|bilingual)\b/.test(n))) return null;
  if (q.controlType !== "checkboxGroup" && q.kind !== "multiChoice") return null;
  // A level asked ("professional working proficiency", "business fluent"):
  // only a language stated at that level; one with no level stated is theirs.
  const levelAsked = /\bproficien\w*|\bfluen\w*|\bbilingual\b/.test(n);
  const spoken = (profile.languages || "")
    .split(/[,;\n]+/)
    .map((l) => l.trim())
    .filter((l) => l && !/\((basic|beginner|elementary|limited|a1|a2)\b/i.test(l))
    .filter((l) => !levelAsked || /\((native|fluent|bilingual|professional|full|advanced|c1|c2|mother tongue|first language)\b/i.test(l))
    .map((l) => qnorm(l.replace(/\(.*$/, "")))
    .filter(Boolean);
  if (spoken.length === 0) return abstain("language:unknown");
  // One option per language, its own: Gallup's 539 (Greenhouse, live, round
  // 7) ticked every option holding the word, fourteen "English (…)"s,
  // "Creoles and pidgins, English based" and "German, Old High" among them,
  // under "…which languages you speak in addition to English". The bare
  // name, else its one variant for the applicant's country ("Tamil
  // (India)"); none the question leaves out.
  const left = /\b(?:in addition to|other than|besides|apart from|aside from|except|excluding|beyond)\s+([a-z]+)\b/.exec(n)?.[1] ?? null;
  const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
  const picks: string[] = [];
  for (const l of spoken) {
    if (l === left) continue;
    const bare = q.options.filter((o) => qnorm(o) === l);
    if (bare.length === 1) {
      picks.push(bare[0]);
      continue;
    }
    const variants = q.options.filter((o) => o.trim().toLowerCase().startsWith(`${l} (`));
    if (variants.length === 1) {
      picks.push(variants[0]);
      continue;
    }
    const mine = variants.filter((o) => {
      const paren = /\(([^)]*)\)/.exec(o)?.[1] ?? "";
      return home !== null && countryFromName(paren)?.code === home;
    });
    if (mine.length === 1) picks.push(mine[0]);
  }
  return picks.length > 0 ? answer([...new Set(picks)].join(", "), "language:choice") : abstain("language:no-matching-option");
}

/**
 * "Are you open to relocation?" answered with WHERE (Twitch, live 2026-10-03:
 * "No", "No, but I'm open to a remote position", "San Francisco, CA", …):
 * willing → the job's own city when it is offered; not willing → "No", or the
 * remote alternative for an applicant who prefers remote work.
 */
function resolveRelocationChoice(q: QuestionInput, n: string, profile: UserApplicationProfile, ctx: QuestionContext): QuestionResult {
  if (!/\b(willing|open|able|prepared) to (relocate|move)\b|\brelocat(e|ion)\b/.test(n) || /\b(assistance|package|support|expenses?|reimburse)\b/.test(n)) return null;
  const opts = (q.options ?? []).filter((o) => o.trim());
  const places = opts.filter((o) => optionPolarity(o) === null && !/\b(remote|anywhere|other|none|n\/a)\b/i.test(o));
  if (places.length < 2) return null;
  const willing = polarityOf(profile.willingToRelocate || "");
  if (willing === false) {
    const remote = opts.filter((o) => optionPolarity(o) === false && /\bremote\b/i.test(o));
    if (/^remote$/i.test((profile.workPreference || "").trim()) && remote.length === 1) return answer(remote[0], "relocation:choice-remote");
    const no = opts.filter((o) => /^no$/i.test(o.trim()));
    return no.length === 1 ? answer(no[0], "relocation:choice-no") : abstain("relocation:no-matching-option");
  }
  if (willing === true && ctx.jobCity) {
    const city = qnorm(ctx.jobCity);
    const hit = places.filter((o) => (" " + qnorm(o) + " ").includes(" " + city + " "));
    if (hit.length === 1) return answer(hit[0], "relocation:choice-job-city");
  }
  return abstain("relocation:choice-unknown");
}

/** "If you live in one of these states, …": a question for residents of the places just listed. */
const LISTED_CONDITION =
  /\bif you (currently )?(live|reside|are located|are based|are living|are residing) in (one of |any of )?(these|those|the (above|listed|following)) (states|provinces|countries|locations|regions|areas)\b/i;

/**
 * "Preference will be given to qualified candidates who do not reside, or plan
 * to reside, in Alabama, Arkansas, … or Tennessee.If you live in one of these
 * states, would you consider relocation?" [Yes | No | N/A, I do not live in
 * one of these states] (Intuitive, SmartRecruiters, live, round 8: blank for
 * a Seattle and an Austin applicant). Someone living in none of the places
 * takes the not-applicable option; someone living in one answers the question
 * after the condition.
 */
function resolveListedPlaces(q: QuestionInput, raw: string, facts: ProfileFacts, profile: UserApplicationProfile, ctx: QuestionContext): QuestionResult {
  const m = LISTED_CONDITION.exec(raw);
  if (!m) return null;
  const regions = new Set<string>();
  const countries = new Set<string>();
  for (const piece of raw.slice(0, m.index).split(/[,;:()]|\.(?=\s|[A-Z]|$)|\s(?:or|and)\s/)) {
    const words = piece.trim().split(/\s+/).filter(Boolean);
    // The place ends the piece ("…reside, in Alabama"): its last words.
    for (let k = Math.min(4, words.length); k >= 1; k--) {
      const said = words.slice(-k).join(" ");
      const region = regionFromText(said);
      if (region && geoNorm(said) === geoNorm(region.name)) {
        regions.add(`${region.country}:${region.code}`);
        break;
      }
      const country = countryFromName(said);
      if (country && geoNorm(said) === geoNorm(country.name)) {
        countries.add(country.code);
        break;
      }
    }
  }
  if (regions.size + countries.size < 2) return null;
  const opts = (q.options ?? []).filter((o) => o.trim());
  const na = opts.filter((o) => !/^(yes|no)\b/i.test(o.trim()) && NOT_APPLICABLE.test(qnorm(o)));
  const loc = facts.location;
  if (!isHigh(loc.country)) return abstain("listed-places:unknown");
  const country = loc.country.value.code;
  const home = isHigh(loc.region) ? `${loc.region.value.country}:${loc.region.value.code}` : null;
  const lives = countries.has(country) || (home !== null && regions.has(home));
  // Outside them: a state named elsewhere, or a country none of them is in.
  const outside = !lives && (home !== null || ![...regions].some((r) => r.startsWith(`${country}:`)));
  if (outside) return na.length === 1 ? answer(na[0], "listed-places:not-a-resident") : abstain("listed-places:not-a-resident");
  if (!lives) return abstain("listed-places:unknown");
  const rest = raw.slice(m.index + m[0].length).replace(/^\s*[,;:]?\s*/, "").trim();
  if (!/\?/.test(rest)) return abstain("listed-places:resident");
  const inner = resolveQuestion({ ...q, label: rest, options: opts.filter((o) => !na.includes(o)) }, facts, profile, ctx);
  return inner?.status === "answer" ? inner : abstain("listed-places:resident");
}

const MONTHS_FULL = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];

/**
 * A start the posting sets, against the stated earliest start, read over the
 * whole text: "This Internship requires you to be able to start no later than
 * Monday January 4th, 2027. Are you able to start on this date?" (Pebl on
 * Ashby, live, round 8: blank for someone who starts in May). A weekday and a
 * day of the month are allowed; "before" excludes the day, "by", "no later
 * than" and "on" include it.
 */
function resolveStartBound(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!isBooleanQuestion(q)) return null;
  const m = /\b(?:start|begin|join|commence)\w*\b[^?]{0,40}?\b(no later than|by|before|on)\s+(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+)?(january|february|march|april|may|june|july|august|september|october|november|december)\s+(?:(\d{1,2})(?:st|nd|rd|th)?\s+)?((?:19|20)\d{2})\b/.exec(n);
  if (!m || !m[3]) return null;
  const av = facts.availability;
  if (!isHigh(av.earliestStart)) return abstain("start-bound:unknown");
  const day = Date.UTC(Number(m[4]), MONTHS_FULL.indexOf(m[2]), Number(m[3]));
  const limit = m[1] === "before" ? day : day + 86400000;
  return booleanResult(av.earliestStart.value.getTime() < limit, q, "start-bound");
}

/**
 * "Are you available to start immediately? If not, when?" (Regent on Ashby,
 * live, round 8: sent to the AI). Yes for an earliest start within a week,
 * the span "Immediately" stands for in start lists (durationRange); later is
 * No. A window the question names ("immediately or within two weeks") is
 * not this question, nor is a legal right to start ("would you be legally
 * eligible to begin employment immediately?", Twitch: work authorization).
 */
function resolveStartImmediately(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!isBooleanQuestion(q)) return null;
  if (!/\b(start|begin|join|commence|availab)\w*\b[^?]{0,30}\b(immediately|right away|asap|as soon as possible)\b|\bimmediate(ly)? (start|availab\w*)\b/.test(n)) return null;
  if (/\bwithin\b|\b\d+\s*(day|week|month)s?\b|\bnotice\b/.test(n)) return null;
  if (/\b(legal\w*|lawful\w*|eligib\w*|authori[sz]\w*|permitted|allowed|entitled)\b/.test(n)) return null;
  const av = facts.availability.earliestStart;
  if (!isHigh(av)) return abstain("start-immediately:unknown");
  const days = Math.ceil((av.value.getTime() - facts.today.getTime()) / 86400000);
  return booleanResult(days <= 7, q, "start-immediately");
}

/** "Which location are you applying for?": one of the posting's own places. */
const POSTING_LOCATION =
  /\b(which|what) (location|office|site|city|hub|work location)s? (are|is) (you|your application) (applying|interested)\b|\b(location|office|site)s? (that )?you (are |re )?applying (for|to)\b|\bapplying (for|to) (which|what) (location|office)\b/;

/**
 * "Which location are you applying for?" over the posting's own places
 * (decision 93). Azul on Lever (round 7: "Sunnyvale, CA" | "United States -
 * Remote") took the remote option only when the profile's address named its
 * country, through the country fallback meant for "What is your location?".
 * The applicant's own city (its metro) when the posting lists it; else the
 * remote option of the applicant's own country, a role they can do where they
 * live (unless they prefer on-site work); else nothing, and not the AI's to
 * guess: which office elsewhere is the applicant's call.
 */
function resolvePostingLocation(q: QuestionInput, n: string, facts: ProfileFacts, profile: UserApplicationProfile): QuestionResult {
  if (!POSTING_LOCATION.test(n)) return null;
  const opts = (q.options ?? []).filter((o) => o.trim());
  if (opts.length < 2 || isBooleanOptionSet(opts)) return null;
  const loc = facts.location;
  if (!isHigh(loc.country)) return abstain("posting-location:unknown");
  const country = loc.country.value.code;
  const remote = (o: string): boolean => /\bremote(ly)?\b/i.test(o);
  if (isHigh(loc.city)) {
    const city = loc.city.value;
    const region = isHigh(loc.region) ? `${loc.region.value.country}:${loc.region.value.code}` : null;
    const own = opts.filter((o) => {
      if (remote(o)) return false;
      const p = placeOf(o);
      if (!p.city || sameMetro(city, p.city) !== true) return false;
      if (p.country && p.country !== country) return false;
      // The same name in another state is another city ("Portland, ME" for
      // someone in Portland, OR); a neighbour across a state line is not.
      return !(geoNorm(city) === geoNorm(p.city) && p.region && region && p.region !== region);
    });
    if (own.length === 1) return answer(own[0], "posting-location:own-city");
  }
  const onsite = /\b(on ?site|in ?office|in person)\b/i.test(profile.workPreference ?? "");
  const mine = opts.filter((o) => {
    if (!remote(o)) return false;
    const named = countryNamedIn(o.replace(/\bremote(ly)?\b/gi, " "));
    return named !== null && named !== "this-country" && named.code === country;
  });
  if (mine.length === 1 && !onsite) return answer(mine[0], "posting-location:remote-in-country");
  return { status: "abstain", rule: "posting-location:applicant-choice", blockBackend: true };
}

/**
 * U.S. export-control / "U.S. person" status (SpaceX "Citizenship Status",
 * Astranis, Hermeus; live 2026-10-03): U.S. citizen or national, lawful
 * permanent resident, refugee, asylee (DACA). Every one of those may work in
 * the US, so an applicant NOT authorized to work there is none of them: the
 * "Other" / "None of the above" / "Foreign person" option. A citizen or
 * permanent resident of the US picks theirs; anything else stays blank.
 */
// "A United States Citizen" | … | "A protected individual" | "Other" (Shield
// AI on Lever, question bank 2026-10-08).
const US_STATUS_OPTION = /\b(u ?s citizen|united states citizen|citizen of the united states|national of the united states|u ?s national|lawful permanent resident|permanent resident of the u|green card|refugee|asylee|protected individual|daca|u ?s person|foreign person)\b/;
// "Not Applicable" beside the statuses too (AMCA's "ITAR Status" on
// Greenhouse, live, round 7: blank for everyone who is no US person).
const NONE = /^(\(?[a-z]\)?\s+)?other\b|\bnone of the above\b|\bforeign person\b|\bnot a u ?s (person|citizen)\b|^(not applicable|n a|does not apply)$/;
/**
 * "Does the deemed export rule affect your employment by Everpure?" (Greenhouse,
 * live 2026-10-17): the rule governs foreign persons, so a US citizen or
 * permanent resident is not affected. Anyone else is the applicant's to judge
 * (it turns on the technology and on licences, not on the profile).
 */
function resolveDeemedExport(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\bdeemed exports?\b/.test(n) || !isBooleanQuestion(q)) return null;
  const basis = facts.workAuth.byCountry.get("US")?.basis;
  const usPerson = basis === "citizen" || basis === "permanent_resident";
  // "…would you meet the requirements for a deemed export license in order
  // to access EAR-controlled technology?" (Axon on Greenhouse, live, round
  // 7): a US person needs no license at all, so meets them; anyone else's
  // license is the government's to grant.
  if (/\b(meet|satisfy|fulfil+|qualify)\b[^?]{0,40}\brequirements?\b|\bqualify for\b/.test(n)) return usPerson ? booleanResult(true, q, "deemed-export:us-person-meets") : abstain("deemed-export:foreign-person");
  if (!/\b(affect|affects|apply|applies|subject|impact|impacts|restrict|restricts)\b/.test(n)) return null;
  return usPerson ? booleanResult(false, q, "deemed-export:us-person") : abstain("deemed-export:foreign-person");
}

function resolveUsPersonStatus(q: QuestionInput, facts: ProfileFacts, profile: UserApplicationProfile, fullLabel: string = q.label): QuestionResult {
  // "Are you a “U.S. person” as defined under applicable U.S. export-control
  // regulations…?" with Yes / No (Jeffrey M. Consulting on Workable,
  // 2026-10-05): a citizen or permanent resident is; a visa is not.
  // Other wordings over Yes / No (Ashby bank 2026-10-08): "Do you currently
  // qualify as a U.S. person…?" (Gecko), "Do any of the following
  // designations apply to you: U.S. citizen…, lawful permanent resident…?"
  // (Reflect Orbital), "…please confirm whether you fall into one of the
  // three statuses above" (Saronic), and a bare definition of "U.S. Persons"
  // (Cowboy Space's ITAR note). A third "Unsure" option leaves it a yes/no.
  // A long statement is asked by its last sentence; the statuses it lists
  // come before it (Saronic), so they are counted in the whole label.
  const ln = qnorm(q.label);
  const whole = qnorm(fullLabel);
  const terms = [/\bu ?s citizens?\b|\bcitizens? of the united states\b/, /\bpermanent residents?\b|\bgreen card\b/, /\brefugees?\b/, /\basylees?\b/].filter((t) => t.test(whole)).length;
  const asksPerson =
    /\b(are you|is the applicant) an? (u ?s|united states) person\b/.test(ln) ||
    /\bqualif(y|ies) as an? (u ?s|united states) person\b/.test(ln) ||
    // "Are you one of the following: • U.S. Citizen or National. • …" (Woven
    // by Toyota on Lever, question bank 2026-10-08).
    (terms >= 2 && /\b(designations?|statuses|criteria|categories) (apply|applies) to you\b|\bfall (into|under|within) one of\b|\bmeet one of\b|\bare you (one|any) of the following\b/.test(ln)) ||
    (terms >= 2 && /\b(u ?s|united states) persons?\b/.test(whole) && /\b(include|includes|is defined|are defined|means)\b/.test(whole));
  const yesNo = isBooleanQuestion(q) || ((q.options ?? []).some((o) => optionPolarity(o) === true) && (q.options ?? []).some((o) => optionPolarity(o) === false));
  if (asksPerson && yesNo) {
    const us = facts.workAuth.byCountry.get("US");
    if (us?.authorized === false) return booleanResult(false, q, "us-person:not-authorized");
    if (us?.basis === "citizen" || us?.basis === "permanent_resident") return booleanResult(true, q, "us-person");
    if (us?.basis === "work_permit" || us?.basis === "student") return booleanResult(false, q, "us-person:visa");
    return abstain("us-person:unknown");
  }
  const opts = (q.options ?? []).filter((o) => o.trim());
  if (opts.filter((o) => US_STATUS_OPTION.test(qnorm(o))).length < 2) return null;
  const us = facts.workAuth.byCountry.get("US");
  // "Not a US citizen or permanent resident" names the statuses it denies
  // (Accenture Federal, live 2026-10-05): never one of them. Only a denial
  // counts: "…refugees with such status granted, not pending" is the U.S.
  // person (Hermeus, regression 2026-10-05).
  const DENIES = /\bnot (a |an )?(u ?s |united states )?(citizens?|nationals?|persons?|permanent residents?|lawful|green card)\b/;
  const pick = (re: RegExp): QuestionResult => {
    let hits = opts.filter((o) => re.test(qnorm(o)) && (re === NONE || !DENIES.test(qnorm(o))));
    // "I am a citizen of Cuba, Iran, North Korea, or Syria AND I am NOT a U.S.
    // person" beside "None of the above; I am a citizen of a different
    // country" (Snowflake, Ashby bank 2026-10-08): the first only for those
    // citizens, the second only with the citizenship known.
    if (re === NONE && hits.length > 1) {
      const citizenships = [...facts.workAuth.byCountry.entries()].filter(([, a]) => a.basis === "citizen").map(([c]) => c);
      if (citizenships.length === 0) return abstain("us-person-status:citizenship-unknown");
      const embargoed = citizenships.some((c) => ["CU", "IR", "KP", "SY"].includes(c));
      hits = hits.filter((o) => /\b(cuba|iran|north korea|syria)\b/.test(qnorm(o)) === embargoed);
    }
    return hits.length === 1 ? answer(hits[0], "us-person-status") : abstain("us-person-status:no-matching-option");
  };
  if (us?.authorized === false) return pick(NONE);
  // A temporary visa (H-1B, F-1 and its OPT, TN…) makes no one a U.S. person:
  // the H-1B and OPT holders abstained (Astranis, SpaceX; question bank
  // 2026-10-05). A statement naming any U.S.-person status keeps it theirs.
  const stated = (profile.workAuthorization || "").toLowerCase();
  if (/\b(h-?1 ?b|h-?4|l-?1|e-?3|o-?1|tn|f-?1|j-?1|m-?1|opt|cpt|visa)\b/.test(stated) && !/\b(citizen|national|permanent resident|green card|refugee|asylee|asylum|daca)\b/.test(stated)) {
    return pick(NONE);
  }
  // "A United States citizen or national" (Anduril; question bank 2, 2026-10-05).
  // "I am a U.S. person" is a citizen's and a permanent resident's (Snowflake).
  if (us?.basis === "citizen") return pick(/\bu ?s citizen\b|\bunited states citizen\b|\bcitizen (or national )?of the united states\b|^u ?s person\b|^i am an? u ?s person$/);
  if (us?.basis === "permanent_resident") return pick(/\blawful permanent resident\b|\bgreen card\b|^i am an? u ?s person$/);
  return abstain("us-person-status:unknown");
}

/** "What school do you attend?", "Where did you complete your undergraduate degree?" */
function resolveSchoolName(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  // "Please include the university you are currently enrolled in", "Please
  // re-confirm the university you currently attend" (Datacor, DV Trading;
  // question bank 2026-10-05): a graduate's old school was written in both.
  const asks =
    /\b(name of (the |your )?(school|university|college|institution)|which (school|university|college|institution)|what (school|university|college|institution)|where (did|do) you (complete|study|go to school|attend|earn|get|obtain)|school (you are )?(currently )?attending)\b|\b(school|university|college|institution) (that |which )?you (are )?(currently )?(attend|attending|enrolled (in|at)|studying at)\b/.test(n);
  if (!asks || q.kind === "boolean") return null;
  const entries = facts.education.entries;
  let entry = null as (typeof entries)[number] | null;
  // "…currently attending OR did you last attend?" (Palantir on Lever, live
  // 2026-10-03) is the main school either way: in progress, else the latest.
  // So is the past tense, "Which college did you attend?", and a slash for
  // the "or": "…currently attending / did you graduate from?" (SharkNinja,
  // ZipRecruiter; question bank 2026-10-05).
  if (/\bor (did you |have you )?(last |most recently |previously )?(attend(ed)?|graduated?|studied)\b|\b(did|have) you (last |most recently |previously )?(attend(ed)?|graduated?|studied)\b/.test(n)) {
    entry = facts.education.primary;
  } else if (/\b(will you be|you will be) (enrolled|attending|studying)\b/.test(n)) {
    // "At which university will you be enrolled for the Fall 2027 semester?"
    // (RentVision on Workable, 2026-10-05) got every graduate's old school:
    // the school of a degree still running then, else blank.
    entry = entries.find((e) => e.completed === false) ?? null;
    const term = /\b(spring|summer|fall|autumn|winter) (19|20)\d{2}\b/.exec(n);
    if (entry && term) {
      const span = optionMonthSpan(term[0]);
      const g = entry.graduation;
      if (!span || !g || g.latest.getUTCFullYear() * 12 + g.latest.getUTCMonth() < span[0]) entry = null;
    }
    if (!entry) return abstain("school-name:not-enrolled");
  } else if (/\bcurrently attend(ing)?\b|currently enrolled|\b(do|will) you attend\b|\b(school|university|college|institution) you attend\b|\bare you attending\b/.test(n)) {
    entry = entries.find((e) => e.completed === false) ?? null;
    if (!entry) return abstain("school-name:not-enrolled");
  } else if (/\bhighest (degree|level|qualification)\b/.test(n)) {
    // "At which institution did you earn your highest degree?" (National
    // Journal on Lever): the school of the highest degree finished, which a
    // later certificate does not replace.
    const done = entries.filter((e) => e.completed === true && e.rank !== null);
    const top = Math.max(...done.map((e) => e.rank ?? 0));
    const best = done.filter((e) => e.rank === top);
    entry = best.length === 1 ? best[0] : null;
  } else if (/\bundergrad/.test(n)) {
    const bachelors = entries.filter((e) => e.rank === 4);
    entry = bachelors.length === 1 ? bachelors[0] : null;
  } else if (/\bgraduate (degree|school|studies)\b|\bmaster/.test(n)) {
    const grads = entries.filter((e) => (e.rank ?? 0) >= 5);
    entry = grads.length === 1 ? grads[0] : null;
  } else {
    entry = facts.education.primary;
  }
  if (!entry?.school) return abstain("school-name:unknown");
  if (q.options && q.options.length) {
    // By the school's own words: the loose matcher chose "Rhode Island School
    // of Design" for Turing School of Software & Design (SharkNinja, question
    // bank 2026-10-05).
    const hit = snapSchool(q.options, entry.school);
    if (hit) return answer(hit, "school-name");
    // A full list (a native select) without the school: its own "not listed"
    // option. A search box's loaded options are only what matched the search.
    // A school listed once per campus is listed: which campus is theirs
    // (Palantir on Lever: University of Washington - Seattle | - Bothell).
    if (schoolOffered(q.options, entry.school)) return abstain("school-name:which-campus");
    // "…If unlisted, please select 'Other'" (ServiceNow on SmartRecruiters,
    // live, round 8: blank for every school abroad): the page says what a
    // school it does not list takes (decision 95).
    if (/\bif (it is |its |your (school|university|college|institution) is )?(unlisted|not listed|not (in|on) (the|this) list)\b[^.?]{0,40}\b(select|choose|pick)\b|\bif you (do not|dont|cannot|cant) (see|find) (it|your (school|university|college|institution))\b[^.?]{0,40}\b(select|choose|pick)\b/.test(n)) {
      const other = q.options.filter((o) => /^other\b/i.test(o.trim()));
      if (other.length === 1) return answer(other[0], "school-name:unlisted-other");
    }
    if (q.controlType === "select") {
      const unlisted = q.options.filter((o) => /\bnot (listed|in (the|this) list|found)\b|\bunlisted\b/i.test(o));
      if (unlisted.length === 1) return answer(unlisted[0], "school-name:not-listed");
    }
    return abstain("school-name:no-matching-option");
  }
  return answer(entry.school, "school-name");
}

// ----- Employment ----------------------------------------------------------------

const CORP_SUFFIX = /\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|group|holdings|technologies|technology)\b/g;
const companyKey = (s: string): string => qnorm(s).replace(CORP_SUFFIX, " ").replace(/\s+/g, " ").trim();

function sameCompany(a: string, b: string): boolean {
  const x = companyKey(a);
  const y = companyKey(b);
  if (!x || !y) return false;
  return x === y || (x.length >= 4 && y.length >= 4 && (` ${x} `.includes(` ${y} `) || ` ${y} `.includes(` ${x} `)));
}

/**
 * "SpaceX & SpaceXAI Employment History" (live 2026-10-03): the label names the
 * company and only the OPTIONS say it is asking whether you worked there ("I
 * have never worked for SpaceX, …", "I am a former SpaceX … employee"). The
 * never-worked option, when no employer on the profile is one of the companies
 * the label names.
 */
function resolveEmploymentHistoryChoice(q: QuestionInput, n: string, raw: string, facts: ProfileFacts): QuestionResult {
  if (!/\bemployment history\b|\bwork history\b/.test(n) || !q.options?.length) return null;
  const never = q.options.filter((o) => /\b(never (worked|been employed)|have not (worked|been employed)|no prior employment)\b/.test(qnorm(o)));
  if (never.length !== 1) return null;
  const names = (raw.match(/[A-Z][\w.'-]*/g) ?? []).filter((w) => !/^(Employment|History|Work|The|And|Of)$/.test(w));
  if (names.length === 0 || facts.employment.employers.length === 0) return abstain("former-employee:no-history");
  const worked = facts.employment.employers.some((e) => names.some((c) => sameCompany(e, c)));
  return worked ? abstain("former-employee:history-choice") : answer(never[0], "former-employee:never");
}

/** Capitalized words in an employer question that are not a company's name. */
const NOT_A_COMPANY =
  /^(are|were|have|has|had|do|did|you|your|current|currently|former|formerly|past|previous|previously|prior|employee|employees|employed|intern|interns|vendor|vendors|contractor|contractors|temp|temps|member|members|extended|workforce|team|including|include|and|or|other|its|their|subsidiaries|subsidiary|affiliates|affiliate|company|companies|please|if|yes|no|i|the|a|an|of|required|select|any|all|e\.?g\.?)$/i;

/** Every company a question names ("…Alphabet employee … (including Google and
 *  other Alphabet subsidiaries)"): a job at any of them is a job there. */
function companiesNamedIn(raw: string): string[] {
  const found = (raw.match(/[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,2}/g) ?? [])
    .map((s) => s.split(/\s+/).filter((w) => !NOT_A_COMPANY.test(w.replace(/[,.;:]+$/, ""))).join(" ").replace(/[,.;:]+$/, ""))
    .filter((s) => s.length >= 2);
  return [...new Set(found)];
}

function resolveFormerEmployee(q: QuestionInput, n: string, raw: string, facts: ProfileFacts, ctx: QuestionContext): QuestionResult {
  // "Are you currently employed?" (Saalex on Workable, 2026-10-05) asks about
  // any job: it was read as "a current employee of us?" and left blank.
  if (/\b(are you|you are|is the applicant) (currently|presently) (employed|working)\b/.test(n) && !/\b(of|by|for|at|with)\b/.test(n)) return null;
  const history = resolveEmploymentHistoryChoice(q, n, raw, facts);
  if (history) return history;
  // A hire that has not happened asks nothing about the past: "I understand
  // also, if employed by Wellmark, I am required to abide by its Code of
  // Conduct…" (Wellmark's certification, SmartRecruiters bank, round 7) was
  // read as "have you been employed by Wellmark?" and answered No.
  const hypothetical = /\bif (i am |you are |i was |you were |i become |you become )?(employed|hired|selected|engaged)\b/;
  if (hypothetical.test(n)) {
    n = n.replace(new RegExp(`${hypothetical.source}( (by|with|at) [^,;.?]+)?`, "g"), " ");
    raw = raw.replace(/\bif (I am |you are |I was |you were |I become |you become )?(employed|hired|selected|engaged)\b( (by|with|at) [^,;.?]+)?/gi, " ");
  }
  // "HISTORY WITH ANDURIL" [Yes | No] (Anduril on Greenhouse, live
  // 2026-10-05): a history with the company named, its jobs first.
  const withCo = /^(?:employment |work |prior |previous )?history with ([A-Za-z][\w&.'-]*(?:\s+[A-Za-z][\w&.'-]*){0,2})$/i.exec(raw.replace(/[\s*✱:?]+$/, "").trim());
  if (withCo && isBooleanQuestion(q)) {
    if (facts.employment.employers.length === 0) return abstain("former-employee:no-history");
    const co = withCo[1];
    const cur = facts.employment.currentCompany;
    const worked = Boolean(isHigh(cur) && sameCompany(cur.value, co)) || facts.employment.employers.some((e) => sameCompany(e, co));
    return booleanResult(worked, q, "former-employee:history-with");
  }
  const shape =
    // "Have you worked with us before?" (Paylocity, live 2026-10-05); "worked
    // with" anything else is usually a skill.
    // "Are you an internal employee of Flourish Research…?" (Workable bank).
    // "Are you currently working with Amplify Education, Inc.?" (Ashby, live,
    // round 8) too: working WITH a company, when the name is the hiring
    // company's or a company's (below).
    /\b(current|former|past|previous|prior|internal|existing)(ly)?\b[^?]*\b(employee|employed|worked|contractor)\b|\bworked (for|at) (us|\w+)|\bworked with us\b|\b(ever|previously) (been )?(employed|worked)\b|\bemployed by\b|\b(provided|done|performed|did) (any )?(contract |consulting |freelance )?(work|services) for\b|\b(currently|presently) work (for|at)\b|\b(currently|presently) working (for|at|with)\b/.test(n) ||
    // A job there: "Have you had a job or internship paid by LLNL?" (LLNL,
    // SmartRecruiters bank, round 7) was read as "have you had an
    // internship?", Yes for three applicants who never worked there.
    // ("How did you learn about this specific role at Kobie?" is no such thing.)
    /\b([Hh]ave|[Dd]id) you (ever |previously |already )?(had|have|held|hold|done|do|completed|complete)\b[^?]{0,20}\b(jobs?|internships?|co-?ops?|positions?|roles?)\b[^?]{0,30}\b(paid by|at|with|for)\s+(?:the\s+)?[A-Z]/.test(raw);
  // ("Do you currently work for Gopuff?", Lever bank 2026-10-08, the last.)
  if (!shape) return null;
  // ("…any of the Crest family of companies", Crest on Lever, live
  // 2026-10-08, is a group of employers, not a relative.)
  // A partner's or reseller's employee is not the company's ("Do you
  // currently work for a partner or reseller of Geotab?", Greenhouse bank).
  // (Only "… of <company>": Waymo's "…Alphabet employee, intern, vendor,
  // contractor…" still asks about Alphabet.)
  if (/\b(relative|family(?! of (companies|brands|businesses))|friend|spouse|referr\w*|government|federal|military|public sector)\b|\b(partners?|resellers?|vendors?|suppliers?|clients?|customers?|competitors?) (of|to|for)\b/.test(n)) return null;
  // The company: a capitalized name in the question ("…employee of ActioNet",
  // "a Twitch employee", "…of any of the Crest family of companies"), else an
  // explicit pointer at the hiring company ("for us", "this company").
  // Anything else ("worked at a startup") names no company we can check.
  // ("…employed by, or contracted to, Ross Video…" names it after a comma;
  // "…by any of our Range Group companies" after "our": Jobvite and
  // Greenhouse, 2026-10-17.)
  // A name may start with digits ("1Password", "3M"; Ashby, live, round 8).
  const NAME = String.raw`(?:[A-Z][\w&.'-]*|\d+[A-Za-z][\w&.'-]*)`;
  // Employed, engaged or contracted WITH a company names it too ("…employed
  // with Amplify?", "…engaged with Pebl…?"; Ashby, live, round 8); "worked
  // with" anything else is usually a skill.
  const viaWith = new RegExp(String.raw`\b(?:employed|engaged|contracted|working)\s+with\s+(?:the\s+)?(${NAME}(?:\s+[A-Z][\w&.'-]*){0,3})`).exec(raw);
  const named =
    new RegExp(String.raw`\b(?:of|by|for|at|to)\s*,?\s+(?:any of\s+)?(?:(?:the|our)\s+)?(${NAME}(?:\s+[A-Z][\w&.'-]*){0,3})`).exec(raw) ??
    viaWith ??
    // "a current MongoDB employee" (MongoDB's embedded form, live 2026-10-03):
    // a qualifier may sit between the article and the name, or two ("a
    // current or former Alphabet employee", Waymo, live 2026-10-05).
    new RegExp(String.raw`\b(?:a|an)\s+(?:(?:current|former|past|previous|prior|full[- ]time|part[- ]time)(?:\s*(?:or|and|\/)\s*(?:current|former|past|previous|prior))?\s+)?(${NAME}(?:\s+[A-Z][\w&.'-]*){0,2})\s+(?:employee|contractor|intern)\b`).exec(raw);
  // Working with something that is no company ("…working with Python?") is a
  // skill: only the hiring company, or a name a company carries, counts.
  if (named && named === viaWith && /\bworking\s+with\b/i.test(named[0]) && !(ctx.company && sameCompany(named[1], ctx.company)) && !/\b(inc|llc|ltd|corp|corporation|company|co|plc|gmbh|group)\b\.?/i.test(named[1] + raw.slice((named.index ?? 0) + named[0].length, (named.index ?? 0) + named[0].length + 12))) return null;
  const pointsHere = /\b(for us|with us|here|this company|our company|the company|this organi[sz]ation|our organi[sz]ation)\b/.test(n);
  // "Are you a current or former employee?" (Carvana's embedded Greenhouse,
  // live 2026-10-05) names no company and no other place: on the employer's
  // own form it asks about the employer.
  const bare =
    /\b(are|were) you (a |an )?((current|former|past|previous|prior)( or | and | ))+(employee|team member|intern|contractor)s?\b/.test(n) &&
    !/\b(of|at|with|for|by|from|government|agency)\b/.test(n);
  const company = named && !/^(us|our|the|this|any|a|an)$/i.test(named[1])
    ? named[1].replace(/[,.]+$/, "")
    : pointsHere || bare
      ? ctx.company
      : "";
  if (!company) return abstain("former-employee:no-company");
  if (facts.employment.employers.length === 0) return abstain("former-employee:no-history");
  const companies = [company, ...(named ? companiesNamedIn(raw) : [])];
  const current = facts.employment.currentCompany;
  const isCurrent = Boolean(isHigh(current) && companies.some((c) => sameCompany(current.value, c)));
  const isPast = !isCurrent && facts.employment.employers.some((e) => companies.some((c) => sameCompany(e, c)));
  const opts = q.options ?? [];
  if (opts.length > 0 && !isBooleanOptionSet(opts)) {
    const pick = (re: RegExp): string | null => {
      const hits = opts.filter((o) => re.test(qnorm(o)));
      return hits.length === 1 ? hits[0] : null;
    };
    // "Current or Former member of Alphabet extended workforce" (Waymo) says
    // both: the option saying only the one that holds wins.
    const only = (re: RegExp, other: RegExp): string | null => {
      const hits = opts.filter((o) => re.test(qnorm(o)) && !other.test(qnorm(o)));
      return hits.length === 1 ? hits[0] : pick(re);
    };
    const v = isCurrent
      ? only(/\bcurrent\b/, /\b(past|former|previous|prior)\b/)
      : isPast
        ? only(/\b(past|former|previous|prior)\b/, /\bcurrent\b/)
        : pick(/\b(neither|none|no|not|never)\b/);
    return v ? answer(v, "former-employee:choice") : abstain("former-employee:no-matching-option");
  }
  // Written: "Have you ever worked for ConsumerAffairs? If yes, what was your
  // position…?" (Workable, live 2026-10-05). No such job: "No". One there is
  // the applicant's to describe.
  if (!q.options?.length && (q.kind === "text" || q.kind === "longText")) {
    return isCurrent || isPast ? null : answer("No", "former-employee:never-text");
  }
  if (!isBooleanQuestion(q)) return null;
  // "Are you a CURRENT employee?" vs "have you EVER worked here?"
  const asksCurrentOnly = /\b(currently|current) (employed|employee|work)\b/.test(n) && !/\b(former|past|previous|ever|or)\b/.test(n);
  return booleanResult(asksCurrentOnly ? isCurrent : isCurrent || isPast, q, "former-employee");
}

/**
 * One box for either: "Current Company / School" (Nutanix on Jobvite,
 * question bank 2026-10-17). The school went in for everyone, a senior
 * engineer's of 2014 included. A job still running is the answer, else the
 * school the applicant is enrolled at, else there is nothing current to name.
 */
function resolveCompanyOrSchool(q: QuestionInput, facts: ProfileFacts): QuestionResult {
  if (q.options?.length) return null;
  const label = q.label.toLowerCase();
  if (!/\b(company|employer)\s*(\/|\bor\b)\s*(school|university|college|institution)\b|\b(school|university|college)\s*(\/|\bor\b)\s*(company|employer)\b/.test(label)) return null;
  const emp = facts.employment;
  if (emp.currentlyEmployed?.value === true && isHigh(emp.currentCompany)) return answer(emp.currentCompany.value, "company-or-school:employer");
  const edu = facts.education;
  if (edu.currentlyEnrolled?.value === true && edu.primary?.school) return answer(edu.primary.school, "company-or-school:school");
  return abstain("company-or-school:none-current");
}

function resolveCurrentlyEmployed(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  if (!/\bare you (currently|presently) (employed|working)\b|\bcurrently employed\b/.test(n) || !isBooleanQuestion(q)) return null;
  if (/\bby\b|\bat\b|\bwith\b/.test(n)) return null;
  const e = facts.employment.currentlyEmployed;
  return isHigh(e) ? booleanResult(e.value, q, "currently-employed") : abstain("currently-employed:unknown");
}

// ----- Availability -------------------------------------------------------------

const pad = (x: number): string => String(x).padStart(2, "0");

/** Format a date the way this field asks for it (placeholder, input type). */
export function formatDateFor(d: Date, q: QuestionInput): string {
  const y = d.getUTCFullYear();
  const m = d.getUTCMonth() + 1;
  const day = d.getUTCDate();
  if (q.inputType === "date") return `${y}-${pad(m)}-${pad(day)}`;
  if (q.inputType === "month") return `${y}-${pad(m)}`;
  // The format the box shows, else the one its question states: "(JJ/MM/AAAA)
  // … (DD/MM/YYYY)" (RodeoFX, SmartRecruiters bank, round 7) got 11/30/2026.
  switch (statedDateFormat(q.placeholder || "") ?? statedDateFormat(q.label || "")) {
    case "DD/MM/YYYY":
      return `${pad(day)}/${pad(m)}/${y}`;
    case "YYYY-MM-DD":
      return `${y}-${pad(m)}-${pad(day)}`;
    case "MM/YYYY":
      return `${pad(m)}/${y}`;
    case "MM/YY":
      return `${pad(m)}/${String(y).slice(2)}`;
    default:
      return `${pad(m)}/${pad(day)}/${y}`;
  }
}

/** The date format a placeholder or question states, if any. */
export function statedDateFormat(text: string): "DD/MM/YYYY" | "MM/DD/YYYY" | "YYYY-MM-DD" | "MM/YYYY" | "MM/YY" | null {
  const t = text.toLowerCase();
  if (/\b(dd|jj)[./-]mm[./-](yyyy|aaaa|yy|aa)\b/.test(t)) return "DD/MM/YYYY";
  if (/\bmm[./-](dd|jj)[./-](yyyy|aaaa|yy|aa)\b/.test(t)) return "MM/DD/YYYY";
  if (/\b(yyyy|aaaa)[./-]mm[./-](dd|jj)\b/.test(t)) return "YYYY-MM-DD";
  if (/\bmm[./-](yyyy|aaaa)\b/.test(t)) return "MM/YYYY";
  if (/\bmm[./-](yy|aa)\b/.test(t)) return "MM/YY";
  return null;
}

function resolveAvailability(q: QuestionInput, n: string, facts: ProfileFacts): QuestionResult {
  const av = facts.availability;
  // "Are there any days you absolutely cannot work? Please highlight any
  // partial availability" (Glossier, question bank 2026-10-05) got the start
  // date: a weekly schedule, which no profile holds.
  if (/\b(days|hours|shifts|times) (that )?you (absolutely )?(cannot|can not|can t|are unable to|are not able to) work\b|\bpartial availability\b|\bweekly (availability|schedule)\b|\bwhich days\b/.test(n)) {
    return abstain("availability:schedule-unknown");
  }
  // "I am available to begin a potential full-time role before September
  // 2028" (Coinbase, question bank 2026-10-05): the earliest start against it.
  const bound = /\b(available|able) to (start|begin|join|commence)\b[^?]*\b(before|by|no later than)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+((?:19|20)\d{2})\b/.exec(n);
  if (bound && isBooleanQuestion(q)) {
    if (!isHigh(av.earliestStart)) return abstain("start-before:unknown");
    const month = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"].indexOf(bound[4]);
    const year = Number(bound[5]);
    // "before September" ends with August; "by September" includes it.
    const limit = bound[3] === "before" ? Date.UTC(year, month, 1) : Date.UTC(year, month + 1, 1);
    return booleanResult(av.earliestStart.value.getTime() < limit, q, "start-before");
  }
  // "Year you expect to begin full time employment" (Jane Street, question
  // bank 2026-10-05): the earliest start's year.
  if (/\byear\b/.test(n) && /\b(begin|start|commence)\w*\b[^?]*\b(full ?time|employment|work|working|career)\b/.test(n) && !/\b(graduat|end|finish)\w*\b/.test(n)) {
    if (!isHigh(av.earliestStart)) return abstain("start-year:unknown");
    const y = String(av.earliestStart.value.getUTCFullYear());
    if (q.options?.length) {
      const hit = q.options.filter((o) => new RegExp(`^\\s*${y}\\s*$`).test(o));
      return hit.length === 1 ? answer(hit[0], "start-year") : abstain("start-year:no-matching-option");
    }
    return answer(y, "start-year");
  }
  // "Are you available to start within 2 weeks / by January 5?"
  const within = /\b(start|begin|join|available)\b[^?]*\bwithin\s+(\d+)\s*(day|week|month)s?\b/.exec(n);
  if (within && isBooleanQuestion(q)) {
    if (!isHigh(av.earliestStart)) return abstain("start-within:unknown");
    const days = Number(within[2]) * (within[3] === "day" ? 1 : within[3] === "week" ? 7 : 30);
    const limit = new Date(facts.today.getTime() + days * 86400000);
    return booleanResult(av.earliestStart.value <= limit, q, "start-within");
  }
  // AVAILABILITY wording only: a bare "Start Date" is just as often an
  // employment row's start date (category experienceStartDate), which this
  // must never answer with the applicant's availability.
  const startQ =
    (q.category === "startDate" ||
      /\b(earliest|when (can|could|would) you (start|begin|join|commence)|when (are|would) you (be )?(able|available) to (start|begin|join|commence|onboard)|available to (start|begin|join|onboard)|availability|date available|available (from|on|starting)|start availability|joining date|(desired|preferred|potential|anticipated|expected|possible|proposed) (start|starting) date|how soon can you start|when (are|would) you (be )?available|available for (employment|work|hire))\b/.test(n)) &&
    // "…available for employment?" is availability (Kenect on Breezy, live
    // 2026-10-03); an employment row's dates are not.
    !/\b(end|finish|graduat|interview|employment (start|end|dates?|history|record)|position held|worked)\b/.test(n);
  if (startQ && (q.kind === "date" || q.kind === "text") && !(q.options && q.options.length)) {
    if (!isHigh(av.earliestStart)) return abstain("start-date:unknown");
    return answer(formatDateFor(av.earliestStart.value, q), "start-date");
  }
  // A notice PERIOD, not any notice: "…I have read and understand the
  // E-verify notice" (Riot Games, question bank 2026-10-05) asked nothing.
  if (/\bnotice period\b|\b(how much|what) notice\b|\bnotice (do|would|will) you\b|\bnotice (required|to give)\b|\b(your|current|required|giving) notice\b|^notice$/.test(n) && !isBooleanQuestion(q)) {
    if (!av.noticeText) return abstain("notice:unknown");
    if (q.options && q.options.length) {
      const hit = pickOption(q.options, av.noticeText);
      return hit ? answer(hit, "notice:choice") : abstain("notice:no-matching-option");
    }
    return answer(av.noticeText, "notice:text");
  }
  return null;
}

// ----- Stated yes/no facts --------------------------------------------------------

function statedBoolean(q: QuestionInput, value: string | undefined, rule: string): QuestionResult {
  const p = polarityOf(value || "");
  if (p === null) return abstain(`${rule}:unknown`);
  return booleanResult(p, q, rule);
}

/** The country a clearance question's own words belong to ("Public Trust",
 *  "TS/SCI", DOE "Q" are US; "Reliability Status" is Canadian). */
function clearanceCountry(n: string): string | null {
  if (/\bpublic trust\b|\bts sci\b|\btop secret sci\b|\bdoe (l|q)\b|\bpolygraph\b|\bdod\b|\b(u s|us|united states) (government |security )?clearance\b/.test(n)) return "US";
  if (/\breliability status\b|\benhanced reliability\b|\bgovernment of canada\b|\bcanadian (security )?clearance\b/.test(n)) return "CA";
  if (/\bbpss\b|\bnppv\b|\bdeveloped vetting\b|\b(sc|dv) (clearance|cleared)\b/.test(n)) return "GB";
  return null;
}

function resolveStatedFacts(q: QuestionInput, n: string, profile: UserApplicationProfile, facts: ProfileFacts, ctx: QuestionContext): QuestionResult {
  if (!isBooleanQuestion(q)) {
    // "…are you willing to commute and/or relocate for this role? If not,
    // please explain:" in a text box (Relativity, question bank 2026-10-05)
    // got the applicant's city: Yes from someone who will move; the rest is
    // theirs to explain.
    if ((q.kind === "text" || q.kind === "longText") && !q.options?.length && /\b(willing|open|able|prepared) to (\w+ (and|or|and or) )?(relocate|move)\b/.test(n) && !/\b(assistance|package|expenses?|reimburse)\b/.test(n)) {
      return polarityOf(profile.willingToRelocate || "") === true ? answer("Yes", "relocation:text") : abstain("relocation:text-unknown");
    }
    // "Clearance type / level" select: "None" when the applicant holds none.
    // Or the option saying so: "I currently do not have an active security
    // clearance" (Credence on Workable, 2026-10-05).
    if (/\bclearances?\b/.test(n) && q.options?.length && /^none$/i.test((profile.securityClearance || "").trim())) {
      // Only an option saying no more than that: "No clearance but clear
      // background and able to obtain" claims more (Vialogic, bank 2).
      const none = q.options.filter(
        (o) =>
          /^none\b/i.test(o.trim()) ||
          (/\b(do not|dont|don t) (currently )?(have|hold)\b[^.]{0,30}\bclearance\b|^no (active |current )?(security )?clearance\b/i.test(qnorm(o)) &&
            !/\b(but|able|obtain|eligib\w*|willing|interested)\b/i.test(qnorm(o)))
      );
      if (none.length === 1) return answer(none[0], "clearance:none");
    }
    // "Latest Employer Name:" (Saalex on Workable, 2026-10-05): the most
    // recent employer, current or not; a past job's applicant had it blank.
    if ((q.kind === "text" || q.kind === "longText") && !q.options?.length && /^(name of )?(your )?(latest|most recent|last) (employer|company)( name)?\b/.test(n)) {
      const recent = facts.employment.mostRecentCompany;
      return isHigh(recent) ? answer(recent.value, "employer:most-recent") : null;
    }
    // The level the profile names, as an option exactly ("Secret" is "Secret
    // Clearance", never "Interim Secret" or "Top Secret"). A clearance stated
    // with no level ("Active clearance") cannot pick one, and the AI could only
    // guess it (ActioNet's eleven types, a veteran, live 2026-10-03).
    const stated = (profile.securityClearance || "").trim();
    if (/\bclearance (type|level)\b|\b(type|level) of (security )?clearance\b/.test(n) && q.options?.length && stated && !/^none$/i.test(stated)) {
      const level = (s: string): string => qnorm(s).replace(/\b(active|current|currently|held|clearance|security)\b/g, " ").replace(/\s+/g, " ").trim();
      const want = level(stated);
      if (!want) return abstain("clearance:level-unknown");
      const hits = q.options.filter((o) => level(o) === want);
      return hits.length === 1 ? answer(hits[0], "clearance:level") : abstain("clearance:level-unknown");
    }
    return null;
  }
  // Paying for it is no willingness to move: "…able to provide your own
  // housing, relocation, and transportation to the internship site?" (The
  // Exploration Company on Ashby, live 2026-10-08) is the applicant's, like
  // relocation assistance (the day session's decision 2).
  if (/\b(willing|open|able|prepared) to (relocate|move)\b|\brelocat(e|ion)\b/.test(n) && !/\b(assistance|package|support|expenses?|benefits?|allowance|reimburse|own (housing|relocation|transportation|travel)|housing|at your own (cost|expense))\b/.test(n)) {
    if (/\b(live|reside|located|based)\b/.test(n)) return null; // residence shape owns it
    return statedBoolean(q, profile.willingToRelocate, "relocation");
  }
  if (/\bdrivers? (s )?licen[cs]e\b|\bdriving licen[cs]e\b/.test(n) && !/\bnumber\b/.test(n)) {
    // "…a valid U.S. driver's license for at least three (3) consecutive
    // years…" (Nuro, question bank 2026-10-05) got Yes from Toronto and
    // Berlin: the profile says a licence is held, not where, nor for how long.
    if (/\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)( \d+)? (consecutive |full )?(years?|months?)\b/.test(n)) return abstain("drivers-license:duration-unknown");
    const named = countryNamedIn(q.label);
    const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
    if (named && named !== "this-country" && named.code !== home) return abstain("drivers-license:other-country");
    return statedBoolean(q, profile.driversLicense, "drivers-license");
  }
  if (/\bclearance\b/.test(n) && !/\b(customs|credit|medical)\b/.test(n)) {
    const c = (profile.securityClearance || "").trim().toLowerCase();
    // A clearance is a national credential, held where it was granted, taken
    // as the applicant's own country. A question in another country's words
    // ("Public Trust") or on a job abroad is not answered from it: a Canadian
    // profile said Yes to a US contractor's "Active Clearance/Public Trust?"
    // (ActioNet on Jobvite, a real profile, 2026-10-03). "None" holds anywhere.
    const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
    const asked = clearanceCountry(n) ?? ctx.jobCountry;
    const active = /\bactive\b/.test(c);
    const eligible = /\beligible|previously\b/.test(c);
    // A U.S. clearance goes to U.S. citizens: a permanent resident or visa
    // holder is not ABLE to obtain one (Pinpoint, a green-card student, live
    // 2026-10-03). Willingness is another question.
    const usBasis = facts.workAuth.byCountry.get("US");
    // No citizen either: someone not authorized in the US, a stated visa or
    // green card, a stated citizenship elsewhere and none of the US.
    const citizenOf = [...facts.workAuth.byCountry.entries()].filter(([, a]) => a.basis === "citizen").map(([cc]) => cc);
    const statedAuth = (profile.workAuthorization || "").toLowerCase();
    const visaOrPr = /\b(h-?1 ?b|h-?4|l-?1|e-?3|o-?1|tn|f-?1|j-?1|m-?1|opt|cpt|visa|permanent resident|green card)\b/.test(statedAuth) && !/\b(u\.? ?s\.?|united states|american) citizen/.test(statedAuth);
    const notCitizen =
      usBasis?.basis !== "citizen" &&
      (usBasis?.authorized === false ||
        visaOrPr ||
        (usBasis !== undefined && ["permanent_resident", "work_permit", "student", "denied"].includes(usBasis.basis)) ||
        (citizenOf.length > 0 && !citizenOf.includes("US")));
    // Whether one CAN be had: "This position may require security
    // clearance. Can this be achieved?" (Boston Engineering, SmartRecruiters
    // bank, round 7) was read as "do you hold one?", No for everyone.
    // (Willing to obtain one is a willingness, never answered from citizenship.)
    const canObtain = /\b(able|eligible) to (obtain|get|acquire|achieve|hold|maintain)\b|\bcan (this|it|that|one|a clearance|the clearance) be (achieved|obtained|attained|granted)\b|\b(could|can) you (obtain|get|achieve)\b/.test(n);
    // Whatever clearance the profile states, or none: "Are you able to hold a
    // U.S. Security Clearance?" (SoloPulse on Lever, live, round 7) was blank
    // for a Canadian whose profile names no clearance.
    if (asked === "US" && canObtain && notCitizen) {
      return booleanResult(false, q, "clearance:us-citizens-only");
    }
    if (!c) return abstain("clearance:unknown");
    if (c !== "none" && home && asked && asked !== home) return abstain("clearance:other-country");
    // Able to HOLD one is eligibility, like able to obtain one: a citizen who
    // holds none was told No.
    const obtainable = canObtain || /\bwilling to (obtain|get|acquire)\b/.test(n);
    if (obtainable) return eligible || active ? booleanResult(true, q, "clearance:obtainable") : abstain("clearance:obtainable-unknown");
    if (c === "none") return booleanResult(false, q, "clearance");
    if (active) return booleanResult(true, q, "clearance");
    if (eligible && /\b(active|current|hold|have)\b/.test(n)) return booleanResult(false, q, "clearance");
    return abstain("clearance:unknown");
  }
  // "…others that we may choose to speak with" (ConsumerAffairs on Workable,
  // live 2026-10-05) is talking to someone, not a language.
  const lang = /\b(speak(?! (with|to)\b)|fluent|proficient|bilingual|fluency|read and write|written and spoken)\b/.exec(n);
  if (lang) {
    const langs = (profile.languages || "").toLowerCase();
    if (!langs.trim()) return abstain("language:unknown");
    const asked = ["english", "french", "spanish", "german", "mandarin", "cantonese", "portuguese", "italian", "japanese", "korean", "hindi", "arabic", "punjabi", "tagalog", "vietnamese", "russian", "dutch", "polish"].filter((l) => n.includes(l));
    if (asked.length === 0) return abstain("language:unspecified");
    const levelOk = (l: string): boolean | null => {
      const m = new RegExp(`${l}\\s*\\(([^)]*)\\)`, "i").exec(profile.languages || "");
      if (!langs.includes(l)) return false;
      if (!m) return /\b(fluent|proficient|bilingual)\b/.test(n) ? null : true;
      return /\b(native|fluent|bilingual|professional|full|advanced|c1|c2)\b/i.test(m[1]) ? true : null;
    };
    const verdicts = asked.map(levelOk);
    if (verdicts.some((v) => v === null)) return abstain("language:level-unknown");
    if (verdicts.every((v) => v === true)) return booleanResult(true, q, "language");
    return abstain("language:not-listed");
  }
  return null;
}

/** The sentence a label asks (its last question, else its last sentence), as written. */
function askedSentenceOf(label: string): string {
  const parts = sentencesOf(label);
  return [...parts].reverse().find((s) => /\?\W*$/.test(s)) ?? parts[parts.length - 1] ?? "";
}

// ----- Phone country code -----------------------------------------------------------

/** A dialing-code option without its code: "(+1) United States" → "United States". */
function withoutDialCode(option: string): string {
  return option.replace(/\(\s*\+\s?\d{1,4}\s*\)|\+\s?\d{1,4}\b/g, " ").replace(/\s+/g, " ").trim();
}

function resolvePhoneCode(q: QuestionInput, n: string, facts: ProfileFacts, profile: UserApplicationProfile): QuestionResult {
  if (q.category !== "phoneCountryCode" && !/\b(country|dial(ing)?|phone|area) code\b|\bcountry calling\b/.test(n)) return null;
  if (/\barea code\b/.test(n)) return null;
  const home = isHigh(facts.location.country) ? facts.location.country.value : null;
  const phone = (profile.phone || "").trim();
  // The country the number is written for: "+44 20 …" is the United Kingdom
  // wherever the applicant lives. It was the home country, which put a
  // Canadian resident's UK number under Canada's code.
  const named = phone ? phoneCountryName(phone, home) : home?.name ?? null;
  const country = named ? countryFromName(named) : null;
  const callingCode = dialCodeOf(phone) ?? (/^\+/.test(phone) ? null : home ? DIAL_CODES[home.code] ?? null : null);
  if (!q.options || q.options.length === 0) {
    if (q.controlType === "combobox" || q.controlType === "customDropdown") {
      return country ? answer(country.name, "phone-code:country-name") : abstain("phone-code:unknown");
    }
    return callingCode ? answer(`+${callingCode}`, "phone-code:text") : abstain("phone-code:unknown");
  }
  const byCountry = country ? q.options.filter((o) => countryNamedIn(o) !== null && (countryNamedIn(o) as { code: string }).code === country.code) : [];
  if (byCountry.length === 1) return answer(byCountry[0], "phone-code:country");
  // "(+1) United States" beside "(+1) United States Minor Outlying Islands"
  // (iCIMS, Schwab, live 2026-10-17): the option that IS the country's name.
  const exact = byCountry.filter((o) => countryFromName(withoutDialCode(o))?.code === country?.code);
  if (exact.length === 1) return answer(exact[0], "phone-code:country");
  if (callingCode) {
    const byCode = q.options.filter((o) => new RegExp(`\\+\\s?${callingCode}(?!\\d)`).test(o));
    if (byCode.length === 1) return answer(byCode[0], "phone-code:calling-code");
  }
  return abstain("phone-code:no-unique-option");
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/** Questions about opinions, motivations, a referrer's name, or criminal
 *  history: nothing on a profile answers them and no default may (a guess there
 *  reads as the applicant's own statement). Recognized so they are never filled
 *  by a category fallback. Channels, prior applications, relatives, conflicts,
 *  background checks and the like are answered by defaultAnswers.ts. */
const UNANSWERABLE =
  /\bwho referred\b|\breferred by (whom|who)\b|\b(name|names) of (the |your )?(referr\w*|employee)\b|\bdo you think\b|\bin your opinion\b|\bwhat do you think\b|\bwhy (do|are|did|would) you\b|\bdescribe (a|an)\b|\btell us about (a|an|yourself)\b|\bwhat interests you\b|\bcriminal\b|\bconvicted\b|\bfelony\b/;

/** A follow-up conditioned on an earlier ANSWER. */
// "If not currently in the Bay Area, …" (Replit, Ashby bank 2026-10-08) is a
// condition on where the applicant lives (resolveConditional), not a follow-up.
const FOLLOW_UP =
  /^if ([a-z] )?(yes|no|so|other|applicable|not(?! (currently |presently )?(in|located|based|living|residing|near|within)\b)|not applicable|you (answered|selected|chose|checked|said|indicated|replied|ticked|heard)|your answer|your (selection|choice|response)|the answer|any of the above|none of the above|referred|referral)\b/;

// ---------------------------------------------------------------------------
// Conditional questions: "If you <condition>, <question>"
// ---------------------------------------------------------------------------

/** Words that open the question after its condition ("…, please provide…"). */
const MAIN_CLAUSE =
  /^(please|kindly|what|which|how|when|where|who|whom|whose|why|are|is|was|were|have|has|had|do|does|did|will|would|can|could|should|list|provide|enter|state|describe|explain|indicate|specify|tell|share|include|select|choose|name|give|identify|confirm)\b/i;
/** A hypothetical the applicant answers as if it held ("If you are offered the
 *  position, will you require sponsorship?"). */
const HYPOTHETICAL = /\b(hired|selected|offered|successful|chosen|an offer|invited|moved? forward|join(ing)? (us|our|the team))\b/;
/** The auxiliary a condition opens with, and whether it negates. */
const CONDITION_AUX: Record<string, [string, boolean]> = ownKeys({
  are: ["Are", false], were: ["Were", false], have: ["Have", false], had: ["Had", false],
  do: ["Do", false], did: ["Did", false], will: ["Will", false], would: ["Would", false],
  can: ["Can", false], could: ["Could", false],
  arent: ["Are", true], werent: ["Were", true], havent: ["Have", true], hadnt: ["Had", true],
  dont: ["Do", true], didnt: ["Did", true], wont: ["Will", true], cant: ["Can", true], cannot: ["Can", true],
});
/** An option saying the question does not apply ("I am not a current or former
 *  government employee", "N/A"). */
const NOT_APPLICABLE =
  /\b(not applicable|n ?a|does not apply|doesnt apply|i am not|im not|i have not|i havent|i was not|i do not|i dont|i did not|i didnt|never been|none of the above)\b/;

/** "you are a current or former government employee" → "Are you a current or
 *  former government employee?" (and whether the condition was negated). */
function conditionQuestion(condition: string): { question: string; negated: boolean } | null {
  const t = condition.trim().replace(/[’‘]/g, "'").split(/\s+/);
  const first = (t[0] ?? "").toLowerCase();
  let aux: string;
  let negated = false;
  let i = 1;
  if (first === "you're" || first === "youre") aux = "Are";
  else if (first === "you've" || first === "youve") aux = "Have";
  // "not currently in the Bay Area" (Replit): the applicant, unsaid.
  else if (first === "not" && /^((currently|presently) )?(in|located|based|living|residing|near|within)\b/i.test(t.slice(1).join(" "))) {
    return { question: `Are you ${t.slice(1).join(" ")}?`, negated: true };
  }
  else if (first === "you") {
    const known = CONDITION_AUX[(t[1] ?? "").toLowerCase().replace(/'/g, "")];
    if (known) {
      [aux, negated] = known;
      i = 2;
    } else aux = "Do"; // "you currently work, or have previously worked, at X"
  } else return null; // a condition on something else ("If the role is remote, …")
  if ((t[i] ?? "").toLowerCase() === "not") {
    negated = !negated;
    i++;
  }
  const rest = t.slice(i).join(" ");
  return rest ? { question: [aux, "you", rest].join(" ") + "?", negated } : null;
}

/**
 * A question that applies only when a condition on the applicant holds. The
 * condition is answered as its own yes/no question first: false → the option
 * saying it does not apply, or blank; true (or a hypothetical "if hired") →
 * the question itself; unknown → blank. ActioNet on Jobvite (live 2026-10-03):
 * "If you are a current or former government employee, have you recused
 * yourself…?" was answered "No" for an applicant who is neither.
 * undefined = not a condition on the applicant: resolve the label as usual.
 */
function resolveConditional(q: QuestionInput, raw: string, facts: ProfileFacts, profile: UserApplicationProfile, ctx: QuestionContext): QuestionResult | undefined {
  const m = /^\s*if\s+([\s\S]+)$/i.exec(raw);
  if (!m) return undefined;
  const body = m[1];
  let condition = "";
  let rest = "";
  const sep = /[,;]\s*|\s[-–—]\s*/g;
  for (let s = sep.exec(body); s; s = sep.exec(body)) {
    const after = body.slice(s.index + s[0].length);
    if (MAIN_CLAUSE.test(after)) {
      condition = body.slice(0, s.index);
      rest = after.trim();
      break;
    }
  }
  if (!condition) return /^you\b|^youre\b|^you'/i.test(body.trim()) ? abstain("conditional:unparsed") : undefined;
  const restQ: QuestionInput = { ...q, label: rest };
  if (HYPOTHETICAL.test(qnorm(condition))) return resolveQuestion(restQ, facts, profile, ctx);
  const asked = conditionQuestion(condition);
  if (!asked) return undefined;
  const held = resolveQuestion(
    { label: asked.question, controlType: "radioGroup", options: ["Yes", "No"], category: "unknown", kind: "boolean" },
    facts,
    profile,
    ctx
  );
  // A condition kept from the AI keeps its follow-up from it too.
  if (!held || held.status !== "answer") {
    return { status: "abstain", rule: "conditional:unknown", blockBackend: held?.status === "abstain" && held.blockBackend === true };
  }
  if ((held.value === "Yes") !== asked.negated) {
    const inner = resolveQuestion(restQ, facts, profile, ctx);
    // The field's category came from the whole label, its condition included:
    // "If you do require sponsorship…, please list the type of support you may
    // require" (Pinterest, question bank 2026-10-05) is a sponsorship field
    // whose category value, the profile's bare Yes/No, answers the condition,
    // never the question. Legal status is not filled that way.
    if (inner === null && (q.category === "sponsorship" || q.category === "workAuthorization")) {
      return abstain(q.category === "sponsorship" ? "sponsorship:follow-up-open" : "work-auth:follow-up-open");
    }
    // "If you were referred…, what is the employee's full name?": who referred
    // them is theirs to write, never a guess (Renaissance; question bank
    // 2026-10-05: the applicant's own name was typed).
    if (inner?.status !== "answer" && /\breferr/.test(qnorm(condition))) return { status: "abstain", rule: "conditional:referrer-unknown", blockBackend: true };
    return inner;
  }
  const na = (q.options ?? []).filter((o) => o.trim() && !/^(yes|no)$/i.test(o.trim()) && NOT_APPLICABLE.test(qnorm(o)));
  return na.length === 1 ? answer(na[0], "conditional:does-not-apply") : abstain("conditional:does-not-apply");
}

/** Abstentions that only mean "the profile is silent": the default answer, if
 *  one applies, takes over (a profile fact always runs first). */
const DEFAULTABLE = /^(relocation:unknown|former-employee:no-history|former-employee:no-company|age-gate:no-dob)/;

/** A question about the applicant's HIGH school (its name, year, grades), which
 *  no profile education row describes. */
// "Highschool Name & Location:" (Saalex on Workable, 2026-10-05) got the
// city the applicant lives in now.
const HIGH_SCHOOL = /\b(high ?school|secondary school)\b/;
const HIGH_SCHOOL_DETAIL = /\b(name|year|graduat\w*|date|attend\w*|where|which|gpa|grades?|location|city)\b/;

const PLACEHOLDER_OPTION = /^(select|choose|please select|please choose|select an option|select one|--)(\W|$)/i;
/**
 * A question that tells people from bots: "What is the third word in this
 * sentence? *(used to monitor web traffic authenticity)*", "What year was
 * Wellmark founded? *(…)*" (Wellmark, SmartRecruiters bank, round 7: the
 * founding year was offered to the AI). The applicant's to answer, never
 * ours or the AI's (decision 79).
 */
const BOT_CHECK =
  /\b(web )?traffic authenticity\b|\b(bot|spam|robot) (check|detection|prevention|protection|filter)\b|\banti ?(spam|bot)\b|\bcaptcha\b|\bprove (that )?(you are|you re|youre) (a )?(human|not a (robot|bot))\b|\bare you (a )?(robot|bot)\b|\bwhat is the (first|second|third|fourth|fifth|last) word (in|of) this (sentence|question)\b|^what is \d+ ?(plus|minus|times|\+|-|x) ?\d+\b/;

export function resolveQuestion(
  q: QuestionInput,
  facts: ProfileFacts,
  profile: UserApplicationProfile,
  ctx: QuestionContext
): QuestionResult {
  const raw = (q.label || "").trim();
  if (!raw) return null;
  const n = qnorm(raw);
  if (BOT_CHECK.test(n)) return abstain("bot-check");
  // A "Select..." placeholder is no option: "Yes | No | Not Applicable" under
  // one went unanswered (Baselayer; question bank 2, 2026-10-05). Every shape
  // sees the real options.
  if (q.options?.some((o) => PLACEHOLDER_OPTION.test(o.trim()))) q = { ...q, options: q.options.filter((o) => !PLACEHOLDER_OPTION.test(o.trim())) };
  // Conditional follow-ups ("If 'Other' selected…", "If yes, please explain"):
  // what they ask depends on an answer we did not give. A condition on the
  // APPLICANT ("If you are currently enrolled…, what is your GPA?") is an
  // ordinary question (Shield AI on Lever, live 2026-10-03).
  // "Optional: If you answered Yes or Unsure, can you provide further
  // details…" (Immuta on Lever, question bank 2026-10-08) is one too: a
  // citizen's "US Citizen" was written in it.
  if (FOLLOW_UP.test(n) || FOLLOW_UP.test(n.replace(/^optional /, "")) || /^if\s*['"“‘]/i.test(raw) || /^(other|if other|other please (specify|explain|describe)|please specify)$/.test(n)) {
    // "If no, will you require sponsorship in the future?" (Hermeus on Lever,
    // live 2026-10-03) is a question of its own after the condition: answered
    // when the profile settles it, whatever was answered before it.
    // ("If applicable, are you willing to relocate?" too: CRB and ServiceNow,
    // SmartRecruiters bank, round 7, left it blank for everyone.)
    const m = /^\s*if (?:yes|no|so|applicable|relevant)\s*[,:;-]?\s+(.+)$/i.exec(raw);
    if (m && /\?\W*$/.test(m[1]) && /^(will|would|do|does|are|is|can|could|have|has|did)\b/i.test(m[1].trim())) {
      const inner = resolveQuestion({ ...q, label: m[1] }, facts, profile, ctx);
      if (inner?.status === "answer") return inner;
    }
    // "If you answered “Yes” above, please select your current immigration
    // status…" (OnePay, Ashby bank 2026-10-08): a status list answers itself.
    const status = resolveVisaStatusList(q, facts, profile);
    if (status?.status === "answer") return status;
    // "Optional: If you answered Yes or Unsure, can you provide further
    // details about the sponsorship or work authorization support you may
    // need…" (Immuta on Lever, question bank 2026-10-08): the Yes is the
    // sponsorship need. Someone who needs it writes their status; nobody else
    // is asked (a citizen's "US Citizen" was written).
    if ((q.kind === "text" || q.kind === "longText") && !q.options?.length && /^if you (answered|selected|chose|said|indicated) yes\b/.test(n.replace(/^optional /, "")) && /\bsponsor/.test(n)) {
      const need = needsSponsorshipIn(facts.workAuth, ctx.jobCountry ?? null, residenceOf(facts));
      const stated = (profile.workAuthorization ?? "").trim();
      if (isHigh(need) && need.value === true && stated) return answer(stated, "conditional:sponsorship-detail");
    }
    return abstain("conditional-follow-up");
  }
  // "Work Eligibility in the United States 🇺🇸" over "Yes, I'm eligible to
  // work in the United States" | "No, I'm not eligible" (Jane on Ashby, live,
  // round 8): a heading that is the question it heads.
  const heading = /^(?:work|employment) (?:eligibility|authori[sz]ation)(?: status)? in (?:the )?([a-z .]+?)\s*$/.exec(n);
  if (heading && (q.options ?? []).filter((o) => o.trim()).length >= 2) {
    const inner = resolveQuestion({ ...q, label: `Are you eligible to work in ${heading[1]}?` }, facts, profile, ctx);
    if (inner?.status === "answer") return inner;
  }
  const listed = resolveListedPlaces(q, raw, facts, profile, ctx);
  if (listed) return listed;
  const startBound = resolveStartBound(q, n, facts);
  if (startBound) return startBound;
  const startNow = resolveStartImmediately(q, n, facts);
  if (startNow) return startNow;
  const conditional = resolveConditional(q, raw, facts, profile, ctx);
  if (conditional !== undefined) return conditional;
  // An attention check names its answer: "To be considered, please choose
  // option C below." [A | B | C | D] (DISA Technologies on Workable,
  // 2026-10-05) was left blank. Only an option that IS that letter or digit.
  const told = /\b(?:choose|select|pick|mark|check|click)\s+(?:option|answer|the option|the answer|letter|choice)?\s*["“'‘]?([a-z0-9])["”'’]?(?:\s+below)?\W*$/i.exec(raw);
  if (told && q.options?.length) {
    const hit = q.options.filter((o) => o.trim().toLowerCase() === told[1].toLowerCase());
    if (hit.length === 1) return answer(hit[0], "attention-check");
  }
  // By its place: "To confirm you carefully read instructions, please select
  // the SECOND option below" (Kobie on Lever, live 2026-10-08).
  const nth = /\b(?:choose|select|pick|mark|check|click)\s+the\s+(first|second|third|fourth|fifth|sixth|last)\s+(?:option|answer|choice)\b/i.exec(raw);
  if (nth && q.options?.length) {
    const place = ["first", "second", "third", "fourth", "fifth", "sixth"].indexOf(nth[1].toLowerCase());
    const real = q.options.filter((o) => o.trim());
    const pick = nth[1].toLowerCase() === "last" ? real[real.length - 1] : real[place];
    if (pick) return answer(pick, "attention-check");
  }
  // "If necessary, are you willing to relocate? (Please check YES if you
  // already live in the Lincoln, NE area.)" (RentVision on Workable,
  // 2026-10-05): the note says how to answer, the question is the move. Read
  // as "do you live in Lincoln?", everyone willing to move said No.
  const yesIf = /\(?\s*(?:please )?(?:check|select|answer|choose|mark|click|tick|pick) ["“']?yes["”']? if you\b[^.)?]*[.)]?\)?/i.exec(raw);
  if (yesIf && /\brelocat|\bmov(e|ing) to\b/i.test(raw.replace(yesIf[0], " "))) {
    const local = resolveResidence({ ...q, label: yesIf[0] }, qnorm(yesIf[0]), facts, profile);
    if (local?.status === "answer" && optionPolarity(local.value) === true) return local;
    return resolveQuestion({ ...q, label: raw.replace(yesIf[0], " ").replace(/\s+/g, " ").trim() }, facts, profile, ctx);
  }
  // "How did you hear about this position? If referred, by who?" (Kenect on
  // Breezy, live 2026-10-03): an add-on starting with "if" asks only when it
  // applies, so the question is judged by its first sentence.
  const sentences = raw.split(/(?<=\?)\s*/);
  const asked = sentences.length > 1 && /^\s*if\b/i.test(sentences.slice(1).join(" ")) ? qnorm(sentences[0]) : n;
  // A long statement is judged by what it asks: SSCI's certification ("…a
  // consumer credit report or criminal records check may be necessary…",
  // Workable bank, 2026-10-05) asks nothing about a record.
  const words = n.split(" ").length;
  const statement = words >= STATEMENT_WORDS;
  if (UNANSWERABLE.test(statement ? qnorm(askedOfStatement(raw, words)) : asked)) return abstain("unanswerable-from-profile");
  // A phone extension: no profile holds one, and a guess (the AI's, or the
  // phone number again, as Workday's "phone-extension" once got) dials wrong.
  if (/^(phone |telephone )?ext(ension)?( number)?$/.test(asked)) return abstain("phone-extension:not-in-profile");
  // The profile's education rows are post-secondary: their school and year
  // answer the university question, never "High School Name" / "Year of High
  // School Graduation" (Palantir on Lever, live 2026-10-03). A yes/no about a
  // diploma is left to the education-level shapes below.
  if (HIGH_SCHOOL.test(n) && HIGH_SCHOOL_DETAIL.test(n) && !isBooleanQuestion(q)) {
    // Its year, from a list that starts after the applicant's first finished
    // degree: high school ended before that, so the list's "Other" (Palantir's
    // 2020-2030, a 2015 graduate, live 2026-10-03).
    if (/\b(year|graduat\w*|date)\b/.test(n) && q.options?.length) {
      const years = q.options.map((o) => /^\s*((?:19|20)\d{2})\s*$/.exec(o)?.[1]).filter((y): y is string => Boolean(y)).map(Number);
      const other = q.options.filter((o) => /^\s*other\b/i.test(o));
      const done = facts.education.entries.filter((e) => e.completed === true && e.graduation).map((e) => e.graduation!.earliest.getUTCFullYear());
      if (years.length >= 2 && other.length === 1 && done.length && Math.min(...done) < Math.min(...years)) {
        return answer(other[0], "high-school:before-listed-years");
      }
    }
    return abstain("high-school:not-in-profile");
  }

  // A note on what follows a No ("Are you at least 18 years or older? (If no,
  // you may be required to provide authorization to work)", Saalex on
  // Workable, 2026-10-05) asks nothing: read as the question, three adults
  // not authorized in the US said they were under 18.
  // A long statement's work right is the one it asks about: "…before being
  // permitted to commence work with Company… Initial to agree." (Credence's
  // drug-test notice on Workable, 2026-10-05) got the work-authorization
  // statement typed in.
  // A lone option ("I Agree") acknowledges the whole statement, read whole.
  const lone = (q.options ?? []).filter((o) => o.trim()).length === 1;
  const unnoted = statement && !lone
    ? askedOfStatement(raw, words)
    : raw.replace(/\(\s*if (no|yes|not|so)\b[^)]*\)/gi, " ").trim();
  // The rules read a long statement by what it asks: "…agreements I may have
  // already signed with current and former employers… if employed…" in
  // Credence's at-will notice (Workable, live 2026-10-05) was read as "have
  // you worked for Company?" and No typed into its initials box.
  const sq = statement && !lone ? { ...q, label: askedOfStatement(raw, words) } : q;
  const sn = sq === q ? n : qnorm(sq.label);
  const resolved =
    resolveRelocationInstruction(q, raw, facts, profile, ctx) ??
    resolveSanctionedList(q, n, facts) ??
    resolveVisaStatusList(q, facts, profile) ??
    resolvePracticalTraining(sq, sn, profile) ??
    (unnoted !== raw ? resolveWorkAuthorization({ ...q, label: unnoted }, qnorm(unnoted), facts, profile, ctx) : resolveWorkAuthorization(q, n, facts, profile, ctx)) ??
    resolveDeemedExport(sq, sn, facts) ??
    resolveUsPersonStatus(sq, facts, profile, q.label) ??
    resolveLaterResidency(sq, sn, facts) ??
    resolveCitizenship(sq, sn, facts, ctx) ??
    resolveAge(sq, sn, facts) ??
    resolveSchoolMembership(sq, sn, facts) ??
    resolveLocalOrRelocate(sq, facts, profile, ctx) ??
    resolveMovePlan(sq, sn, profile) ??
    resolveResidence(sq, sn, facts, profile) ??
    resolvePostingLocation(sq, sn, facts, profile) ??
    resolveRegionChoice(sq, sn, facts) ??
    resolveLocatedChoice(sq, sn, facts) ??
    resolveFormerEmployee(sq, sn, sq.label, facts, ctx) ??
    resolveRecentEmployer(sq, sn, facts) ??
    resolveCompanyOrSchool(sq, facts) ??
    resolveCurrentlyEmployed(sq, sn, facts) ??
    resolveYearsOfExperience(sq, sn, facts) ??
    resolvePursuedDegree(sq, sn, facts) ??
    resolveClassStanding(sq, sn, facts) ??
    resolveEducationLevel(sq, sn, facts) ??
    resolveDegreeCandidate(sq, sn, facts) ??
    resolveF1Status(sq, sn, profile) ??
    resolveReturningToSchool(sq, sn, facts) ??
    resolveEnrollment(sq, sn, facts) ??
    resolveCoop(sq, sn, facts) ??
    resolvePreviousInternship(sq, sn, profile) ??
    resolveSchoolSchedule(sq, sn, facts) ??
    resolveSchoolAtLevel(sq, sn, facts) ??
    resolveEducationSummary(sq, sn, facts) ??
    resolveGraduatingInTerm(sq, facts) ??
    resolveGraduation(sq, sn, facts) ??
    resolveGpa(sq, sn, profile, facts) ??
    resolveDisciplineAtLevel(sq, sn, facts) ??
    resolveTestScore(sq, sn) ??
    resolveStartDateChoice(sq, sn, facts) ??
    resolveStartBucket(sq, sn, facts) ??
    resolveStartCalendar(sq, sn, facts) ??
    resolveTimezone(sq, sn, facts) ??
    resolveZoneAvailability(sq, sn, facts) ??
    resolveDidGraduate(sq, sn, facts) ??
    resolvePeriodAvailability(sq, sn, facts) ??
    resolveSalaryUnit(sq, sn, facts, profile) ??
    resolveSalaryCurrency(sq, sn, facts, profile, ctx) ??
    resolveLocalTo(sq, sn, facts, profile) ??
    resolveSchoolName(sq, sn, facts) ??
    resolveAvailability(sq, sn, facts) ??
    resolveRelocationChoice(sq, sn, profile, ctx) ??
    resolveLanguageChoice(sq, sn, profile, facts) ??
    resolveStatedFacts(sq, sn, profile, facts, ctx) ??
    resolvePhoneCode(sq, sn, facts, profile) ??
    null;
  // A legal-status Yes in a question that also asks for in-person work
  // somewhere ("Are you a US Citizen or Green Card Holder that can work onsite
  // in Whippany NJ ~3 days per week", Giftogram, question bank 2026-10-05) is
  // No for someone elsewhere who will not move: both halves must hold.
  if (
    resolved?.status === "answer" &&
    /^(work-auth|citizenship|us-person)/.test(resolved.rule) &&
    optionPolarity(resolved.value) === true &&
    onsiteVerdict(askedSentenceOf(q.label), profile, facts, ctx) === "elsewhere"
  ) {
    return booleanResult(false, q, `${resolved.rule}+onsite-elsewhere`);
  }
  if (resolved && (resolved.status === "answer" || !DEFAULTABLE.test(resolved.rule))) return resolved;
  // An adult-applicant gate with no date of birth: 18 or older is the default;
  // a higher bar (21) stays the applicant's to answer.
  // No work history at all: never this company's employee either ("Have you
  // previously worked for D2L in any capacity?", a new graduate, live
  // 2026-10-03). The "no", "never" or "neither" option, or No.
  if (resolved?.rule === "former-employee:no-history") {
    const opts = (q.options ?? []).filter((o) => o.trim());
    if (opts.length === 0 || isBooleanOptionSet(opts)) return booleanResult(false, q, "default:no-history");
    const never = opts.filter((o) => /^(no|never|neither|none|n a|not applicable)\b/.test(qnorm(o)) || /\b(have not|havent|never)\b/.test(qnorm(o)));
    return never.length === 1 ? answer(never[0], "default:no-history") : resolved;
  }
  if (resolved?.rule === "age-gate:no-dob") {
    const min = Number(AGE_MIN.exec(n)?.slice(1).find(Boolean) ?? NaN);
    return min <= 18 && !AGE_UNDER.test(n) ? booleanResult(true, q, "default:adult") : resolved;
  }
  const settled = resolveDefault(q, n, profile, facts, ctx) ?? resolved;
  // The page's own word for a No: "…If so, please provide their name – if
  // not type N/A." (M3USA, SmartRecruiters bank), "…If no or not applicable,
  // type N/A." (National Life on Greenhouse, live); round 7, both blank. The
  // question before it decides; a Yes leaves the details to the applicant.
  if ((q.kind === "text" || q.kind === "longText") && !q.options?.length) {
    const na = /\bif (?:not|no|none|not applicable|no or not applicable|no or n\/?a)\b[^.?]{0,20}?\b(?:type|enter|write|put|respond with|answer)\s*["“'‘]?(N\/A|NA|None|n\/a)["”'’]?/i.exec(raw);
    const first = raw.split("?")[0].trim();
    if (na && settled?.status === "answer" && optionPolarity(settled.value) === false) return answer(na[1], "default:not-applicable-instructed");
    if (na && settled?.status !== "answer" && first && first !== raw.trim()) {
      const inner = resolveQuestion({ ...q, label: `${first}?`, kind: "boolean", options: ["Yes", "No"], controlType: "radioGroup" }, facts, profile, ctx);
      if (inner?.status === "answer" && optionPolarity(inner.value) === false) return answer(na[1], "default:not-applicable-instructed");
    }
  }
  // A bare topic for a label asks what its description asks: "Onsite
  // Availability" over "This job is 5 days a week onsite in El Segundo, CA.
  // Please confirm that you are willing and able to be onsite 5 days a
  // week." (AMCA on Greenhouse, live, round 7) went unanswered for everyone
  // who would move. Read with it when the topic alone settled nothing.
  if (settled?.status !== "answer" && raw.split(/\s+/).length <= 3 && !/\?/.test(raw)) {
    const help = (q.helpText ?? "").trim();
    const asking = sentencesOf(help).filter((s) => /\?\W*$/.test(s) || /^(please )?(confirm|indicate|select|tell us|let us know)\b/i.test(s));
    if (asking.length > 0 && help.split(/\s+/).length <= 80) {
      const read = resolveQuestion({ ...q, label: `${help}`, helpText: "" }, facts, profile, ctx);
      if (read?.status === "answer") return read;
    }
  }
  return settled;
}
