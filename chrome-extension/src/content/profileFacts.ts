/**
 * Deterministic inference layer: facts DERIVED from the profile, each with a
 * confidence, so a form question the profile never states outright can still
 * be answered when, and only when, the profile settles it.
 *
 *   "Toronto, ON, Canada"            → city Toronto, province Ontario (ON), country Canada
 *   "Canadian citizen"               → authorized to work in Canada, no sponsorship there
 *   rows ending "Present"            → the current employer and title
 *   dated rows                       → total years of experience (overlaps counted once)
 *   education rows                   → highest level, school, field, graduation, enrolled?
 *   notice period ↔ earliest start   → each derives the other
 *   date of birth                    → age (and age gates)
 *
 * Confidence is a promise about what happens next, not a score:
 *   high    the profile settles it. Fill it.
 *   medium  likely, but a real person could differ (a bare "Toronto" is probably
 *           Canada; there is also a London, Ontario). Never filled.
 *   low     a hint. Never filled.
 *
 * Pure: no DOM, no clock of its own (`today` is injected), no network.
 */
import type { UserApplicationProfile } from "../shared/types";
import {
  COUNTRIES,
  countryByCode,
  countryFromName,
  countryHintForCity,
  findPostalCode,
  geoNorm,
  regionFromText,
  type Country,
  type Region,
} from "./geo";
// The veto cues of the work-authorization reader, shared with the backend port
// (decide-b round 5): one file, read by both languages.
import VETO_LEXICON from "../../../backend/data/work_auth_veto.json" with { type: "json" };
// The closed vocabulary of the allow-list layer (decide-b, D-61 amendment),
// shared with the backend port the same way.
import ALLOW_LIST from "../../../backend/data/work_auth_allowlist.json" with { type: "json" };

export type Confidence = "high" | "medium" | "low";

export interface Fact<T> {
  value: T;
  confidence: Confidence;
  /** Which rule produced it, for telemetry and the NOTES audit trail. */
  source: string;
}

const fact = <T>(value: T, confidence: Confidence, source: string): Fact<T> => ({ value, confidence, source });

export const isHigh = <T>(f: Fact<T> | null | undefined): f is Fact<T> => Boolean(f && f.confidence === "high");

// ---------------------------------------------------------------------------
// Dates
// ---------------------------------------------------------------------------

/** A partially-known date widened into the span it pins down (mirrors the
 *  backend's derived_facts.DateSpan, so both sides read dates the same way). */
export interface DateSpan {
  earliest: Date;
  latest: Date;
  /** "day" | "month" | "year": how much the source text actually stated. */
  precision: "day" | "month" | "year";
}

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

const utc = (y: number, m: number, d: number): Date => new Date(Date.UTC(y, m, d));
const lastDay = (y: number, m: number): number => new Date(Date.UTC(y, m + 1, 0)).getUTCDate();

export const PRESENT_RE = /\b(present|current|currently|now|ongoing|to date|today)\b/i;

/**
 * Parse a profile date: YYYY-MM-DD, YYYY-MM, YYYY, "Mon YYYY", "Month YYYY",
 * MM/YYYY. Ambiguous all-numeric day/month orders ("03/04/1998") are refused:
 * a coin flip is not a fact.
 */
export function parseDateSpan(text: string): DateSpan | null {
  const t = (text || "").trim();
  if (!t) return null;
  let m: RegExpMatchArray | null;
  if ((m = t.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T\s].*)?$/))) {
    const y = +m[1], mo = +m[2] - 1, d = +m[3];
    if (mo < 0 || mo > 11 || d < 1 || d > lastDay(y, mo)) return null;
    return { earliest: utc(y, mo, d), latest: utc(y, mo, d), precision: "day" };
  }
  if ((m = t.match(/^(\d{4})[-/.](\d{1,2})$/))) {
    const y = +m[1], mo = +m[2] - 1;
    if (mo < 0 || mo > 11) return null;
    return { earliest: utc(y, mo, 1), latest: utc(y, mo, lastDay(y, mo)), precision: "month" };
  }
  if ((m = t.match(/^(\d{1,2})[-/.](\d{4})$/))) {
    const y = +m[2], mo = +m[1] - 1;
    if (mo < 0 || mo > 11) return null;
    return { earliest: utc(y, mo, 1), latest: utc(y, mo, lastDay(y, mo)), precision: "month" };
  }
  if ((m = t.match(/^([A-Za-z]{3,9})\.?,?\s+(\d{4})$/))) {
    const mo = MONTHS.indexOf(m[1].slice(0, 3).toLowerCase());
    if (mo < 0) return null;
    const y = +m[2];
    return { earliest: utc(y, mo, 1), latest: utc(y, mo, lastDay(y, mo)), precision: "month" };
  }
  if ((m = t.match(/^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$/))) {
    const mo = MONTHS.indexOf(m[1].slice(0, 3).toLowerCase());
    if (mo < 0) return null;
    const y = +m[3], d = +m[2];
    if (d < 1 || d > lastDay(y, mo)) return null;
    return { earliest: utc(y, mo, d), latest: utc(y, mo, d), precision: "day" };
  }
  if ((m = t.match(/^(\d{4})$/))) {
    const y = +m[1];
    return { earliest: utc(y, 0, 1), latest: utc(y, 11, 31), precision: "year" };
  }
  return null;
}

const DAY_MS = 86400000;

/** The applicant's own calendar day, as the UTC midnight every parsed date
 *  uses. The UTC day was a day ahead every evening in the Americas: "2 weeks
 *  from today" at 9 p.m. in Ottawa started a day late (2026-10-03). */
function startOfDay(d: Date): Date {
  return utc(d.getFullYear(), d.getMonth(), d.getDate());
}

// ---------------------------------------------------------------------------
// Location
// ---------------------------------------------------------------------------

export interface LocationFacts {
  street: Fact<string> | null;
  city: Fact<string> | null;
  region: Fact<Region> | null;
  postalCode: Fact<string> | null;
  country: Fact<Country> | null;
}

interface ParsedAddress {
  street?: string;
  city?: string;
  region?: Region;
  postal?: { code: string; country: string };
  country?: Country;
}

/**
 * Parse one free-text address/location: "1055 W Georgia St, Vancouver, BC V6E
 * 3P3, Canada", "Toronto, ON, Canada", "Cambridge, MA 02139", "Ottawa".
 * Returns only the parts it could identify.
 */
export function parseAddress(text: string): ParsedAddress {
  const out: ParsedAddress = {};
  const raw = (text || "").trim();
  if (!raw) return out;
  const postal = findPostalCode(raw);
  if (postal) out.postal = postal;
  let parts = raw.split(/\s*[,;\n]\s*/).map((p) => p.trim()).filter(Boolean);

  // Country: the last part naming one.
  for (let i = parts.length - 1; i >= 0; i--) {
    const c = countryFromName(parts[i]);
    if (c) {
      out.country = c;
      parts.splice(i, 1);
      break;
    }
  }
  // Region: a part that IS a state/province, or one that begins with it and
  // carries the postal code ("BC V6E 3P3", "MA 02139").
  const prefer = (out.country?.code === "US" || out.country?.code === "CA" ? out.country.code : undefined) as "US" | "CA" | undefined;
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    const stripped = postal ? p.replace(new RegExp(postal.code.replace(/[\s-]/g, "[\\s-]?"), "i"), "").trim() : p;
    const region = regionFromText(stripped, prefer);
    if (region && (!out.country || out.country.code === region.country)) {
      out.region = region;
      parts.splice(i, 1);
      break;
    }
  }
  // Drop a part that is nothing but the postal code.
  if (postal) parts = parts.filter((p) => p.replace(/[\s-]/g, "").toUpperCase() !== postal.code.replace(/[\s-]/g, "").toUpperCase());
  // Street: a part opening with a civic number ("1055 W Georgia St", "77 Massachusetts Ave").
  const streetIdx = parts.findIndex((p) => /^\d+[A-Za-z]?\b\s+\S/.test(p) || /^(unit|apt|suite)\b/i.test(p));
  if (streetIdx >= 0) {
    out.street = parts[streetIdx];
    parts.splice(streetIdx, 1);
    // An apartment/unit part right after the street belongs to it.
    if (parts[streetIdx] && /^(apt|apartment|unit|suite|ste|#)\b/i.test(parts[streetIdx])) {
      out.street += `, ${parts[streetIdx]}`;
      parts.splice(streetIdx, 1);
    }
  }
  // City: what is left, when exactly one plain-word part remains.
  const words = parts.filter((p) => /^[\p{L}][\p{L}\s.'-]*$/u.test(p));
  if (words.length >= 1) out.city = words[words.length - 1];
  return out;
}

function countryOf(parsed: ParsedAddress): Country | null {
  if (parsed.country) return parsed.country;
  if (parsed.region) return countryByCode(parsed.region.country);
  if (parsed.postal) return countryByCode(parsed.postal.country);
  return null;
}

export function locationFacts(profile: UserApplicationProfile): LocationFacts {
  const fromStreet = parseAddress(profile.addressStreet || "");
  const fromLocation = parseAddress(profile.location || "");
  const streetHasMore = Boolean(fromStreet.city || fromStreet.region || fromStreet.country || fromStreet.postal);

  // Street: the structured field, minus any city/region/country it carried.
  let street: Fact<string> | null = null;
  if (profile.addressStreet?.trim()) {
    street = streetHasMore
      ? fromStreet.street
        ? fact(fromStreet.street, "high", "address:parsed-street")
        : null
      : fact(profile.addressStreet.trim(), "high", "profile:addressStreet");
  }

  // Country: stated > a region/postal code that only exists in one country >
  // a named country in the location string. A bare city is only a hint.
  let country: Fact<Country> | null = null;
  const stated = countryFromName(profile.country || "") ?? (profile.country?.trim().length === 2 ? countryByCode(profile.country.trim()) : null);
  if (stated) country = fact(stated, "high", "profile:country");
  const statedRegion = regionFromText(profile.addressState || "", stated?.code === "US" || stated?.code === "CA" ? (stated.code as "US" | "CA") : undefined);
  if (!country) {
    const c = countryOf(fromStreet) ?? countryOf(fromLocation) ?? (statedRegion ? countryByCode(statedRegion.country) : null);
    if (c) country = fact(c, "high", "address:country");
  }
  if (!country) {
    const cityGuess = profile.addressCity || fromLocation.city || fromStreet.city;
    const hinted = cityGuess ? countryHintForCity(cityGuess) : null;
    const c = hinted ? countryByCode(hinted) : null;
    if (c) country = fact(c, "medium", "city-hint");
  }

  // Region: stated > parsed (only when consistent with the country).
  let region: Fact<Region> | null = null;
  const regionOk = (r: Region | undefined | null): r is Region => Boolean(r && (!country || country.value.code === r.country));
  if (regionOk(statedRegion)) region = fact(statedRegion, "high", "profile:addressState");
  else if (regionOk(fromStreet.region)) region = fact(fromStreet.region, "high", "address:region");
  else if (regionOk(fromLocation.region)) region = fact(fromLocation.region, "high", "location:region");

  // City: stated > the address's city > the location string's city.
  let city: Fact<string> | null = null;
  if (profile.addressCity?.trim()) city = fact(profile.addressCity.trim(), "high", "profile:addressCity");
  else if (fromStreet.city && streetHasMore) city = fact(fromStreet.city, "high", "address:city");
  else if (fromLocation.city) {
    // "Toronto, ON, Canada" pins the city; a lone word could be anything
    // ("Remote", "Greater Toronto Area"), so it is only a hint on its own.
    const pinned = Boolean(fromLocation.region || fromLocation.country || fromLocation.postal);
    if (!/\b(remote|anywhere|hybrid|area|region)\b/i.test(fromLocation.city)) {
      city = fact(fromLocation.city, pinned ? "high" : "medium", "location:city");
    }
  }

  let postalCode: Fact<string> | null = null;
  if (profile.postalCode?.trim()) postalCode = fact(profile.postalCode.trim(), "high", "profile:postalCode");
  else if (fromStreet.postal) postalCode = fact(fromStreet.postal.code, "high", "address:postal");
  else if (fromLocation.postal) postalCode = fact(fromLocation.postal.code, "high", "location:postal");

  return { street, city, region, postalCode, country };
}

// ---------------------------------------------------------------------------
// Work authorization
// ---------------------------------------------------------------------------

export type AuthBasis = "citizen" | "permanent_resident" | "work_permit" | "student" | "statement" | "denied" | "stated";

export interface CountryAuth {
  /** Authorized to work there: true/false, or null when the profile does not say. */
  authorized: boolean | null;
  /** Needs sponsorship now or in the future there. */
  needsSponsorship: boolean | null;
  basis: AuthBasis;
  /** The right is a visa an employer sponsors (H-1B, L-1, E-3, O-1, TN): held
   *  for that employer only, not for any employer. */
  employerTied?: boolean;
}

export interface WorkAuthFacts {
  /** Per ISO country code. Only countries the profile actually speaks about. */
  byCountry: Map<string, CountryAuth>;
  /** The applicant's own yes/no on "do you require sponsorship", if stated. */
  statedSponsorship: boolean | null;
  /** A statement that named NO country ("Yes", "Authorized to work"). */
  unscopedAuthorized: boolean | null;
  /** The applicant wrote a sponsorship answer that reads neither Yes nor No
   *  ("No for Canada, yes for US"): nothing may be inferred in its place. */
  sponsorshipUnclear?: boolean;
  /** Countries whose status a veto cue withheld ("I'm on OPT until 2027", "My
   *  wife is a US citizen"): what the applicant holds there is not known, so a
   *  general "No" to sponsorship is not applied there (decide-b round 5). */
  withheld?: string[];
}

/** "N/A" and its spellings, read against a trimmed, lowercased answer. */
const NOT_APPLICABLE = /^(n\s*[/.]?\s*a\b\.?|not applicable\b|does not apply\b|doesn'?t apply\b)/;
/** An answer that says the applicant does not know. */
const UNCERTAIN_ANSWER = /^(not sure|unsure|not certain|uncertain|unknown|tbd|don'?t know|do not know)\b/;
/** Words that put an answer in the future ("but will need H-1B later"). */
const FUTURE_WORDS = /\b(will|later|future|after|eventually)\b/;

/**
 * Every run of whitespace as one character: a newline when the run holds a
 * line break (a labelled answer keeps its lines), else a space. Applied to
 * user text before any other pattern (decide-b round 4), so no pattern ever
 * meets a long whitespace run: two `\s*` around an optional group were O(n^2)
 * on one (100,000 spaces took 108 s).
 */
export function collapseSpaces(text: string): string {
  return text.replace(/\s+/g, (m) => (/[\n\r]/.test(m) ? "\n" : " "));
}

/** A work-authorization or sponsorship answer is read only up to here (decide-b
 *  round 5). Real answers are short; the cap bounds the worst-case time. */
export const INPUT_CAP = 4000;

/** The first INPUT_CAP characters, counted as code points (the backend's
 *  str), so an emoji counts once in both languages. */
export function capInput(text: string): string {
  if (text.length <= INPUT_CAP) return text;
  let i = 0;
  let n = 0;
  while (i < text.length && n < INPUT_CAP) {
    const cp = text.codePointAt(i) ?? 0;
    i += cp > 0xffff ? 2 : 1;
    n++;
  }
  return text.slice(0, i);
}

/** A leading question echoed before the answer ("Do you require sponsorship? No"). */
const QUESTION_ECHO = /^(do|does|are|is|will|would|have|has|can|could)\b/;

/** Yes/no reading of a short stored answer, or null. */
export function polarityOf(text: string): boolean | null {
  let t = collapseSpaces(capInput(text || "")).trim().toLowerCase();
  if (!t) return null;
  // "Do you now or in the future require sponsorship? No.": the answer is
  // what follows the echoed question (decide-b review3).
  const q = t.lastIndexOf("?");
  if (q >= 0 && QUESTION_ECHO.test(t)) {
    const rest = t.slice(q + 1).trim();
    if (rest) t = rest;
  }
  // "N/A", "NA", "N.A.", "not applicable": unanswered, never a No (owner
  // decisions 2026-10-13; "n" before "/" read as "No").
  if (NOT_APPLICABLE.test(t)) return null;
  // "Not sure", "TBD": unanswered. Never a No (decide-b review I-2).
  if (UNCERTAIN_ANSWER.test(t)) return null;
  if (/^(yes|y|yeah|yep|yup|true|oui|si)\b/.test(t)) return true;
  if (/^(no|n|nope|nah|false|non|none|not)\b/.test(t)) {
    // "Not yet", "Not currently, but I will", "Not required now but will be
    // after OPT": a No today that turns into a Yes later, so neither.
    // "Not at the moment", "Not right now": only about today.
    if (/^not (yet|currently|presently|now|at the moment|right now|at this time|for now)\b/.test(t)) return null;
    // "No (will need after OPT)", "No, until my OPT ends", "No for Canada,
    // yes for US": a later need or a later Yes. "No, I will not need it in
    // the future" stays a No.
    if (/\buntil\b/.test(t) || /\byes\b/.test(t.slice(2))) return null;
    if (FUTURE_WORDS.test(t) && !/\b(will not|won't|never)\b/.test(t)) return null;
    return false;
  }
  if (/\b(will not|won't|do not|don't|does not|doesn't|no longer|never)\s+(need|require)/.test(t)) return false;
  if (/\b(will|do|does|would)\s+(need|require)\b/.test(t)) return true;
  if (/\b(not required|not needed)\b/.test(t)) return false;
  if (/\b(required|needed)\b/.test(t)) return true;
  return null;
}

/** Countries a free-text statement names (by name, alias, or demonym). */
export function countriesIn(text: string): Country[] {
  const norm = ` ${(text || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()} `;
  const found: Country[] = [];
  const seen = new Set<string>();
  const add = (c: Country | null): void => {
    if (c && !seen.has(c.code)) {
      seen.add(c.code);
      found.push(c);
    }
  };
  // Multi-word and unambiguous names first.
  const NAME_PATTERNS: Array<[RegExp, string]> = [
    // Bare "us" is a pronoun ("work for us") unless it follows "the"/"in";
    // "America" is the US only when no continent word precedes it.
    [/ (united states of america|united states|u s a|u s|usa)  ?| (the|in) us | (?<!(north|south|latin|central) )(america|american) /, "US"],
    [/ (canada|canadian) /, "CA"],
    [/ (united kingdom|great britain|britain|british|england|uk|u k) /, "GB"],
  ];
  for (const [re, code] of NAME_PATTERNS) if (re.test(norm)) add(countryByCode(code));
  // "US Citizen": "US" in capitals is the country (lowercase "us" stays a
  // pronoun). Lowercased above, it was missed, and a citizen read as a bare
  // "authorized" (question bank 2026-10-05).
  if (/(^|[^A-Za-z])U\.?S\.?(?![A-Za-z])/.test(text || "")) add(countryByCode("US"));
  // US-only statuses imply the US even when the country is not named.
  if (/ (green card|h ?1 ?b|h ?4|l ?1|tn|e ?3|o ?1|f ?1|j ?1|m ?1|opt|cpt|ead|stem opt) /.test(norm)) add(countryByCode("US"));
  // Canada-only statuses.
  if (/ (pgwp|post graduation work permit|study permit|landed immigrant|pr card) /.test(norm)) add(countryByCode("CA"));
  // Any other country named outright.
  for (const word of norm.trim().split(" ")) {
    // "america(n)" was judged above, with its continent guard.
    if (word.length < 4 || word === "america" || word === "american") continue;
    const c = countryFromName(word);
    if (c) add(c);
  }
  return found;
}

/**
 * Read the free-text work authorization (and the stated sponsorship answer)
 * into per-country facts.
 *
 *   "Canadian citizen"                         CA: authorized, no sponsorship
 *   "Authorized to work in Canada and the U.S." CA, US: authorized
 *   "Permanent resident of Canada"             CA: authorized, no sponsorship
 *   "F-1 student visa (OPT eligible)"          US: authorization unknown (restricted)
 *   "H-1B"                                     US: authorized, needs sponsorship
 *   "Yes"                                      unscoped: authorized
 */
export function workAuthFacts(profile: UserApplicationProfile): WorkAuthFacts {
  const text = normalizeAnswer(profile.workAuthorization || "");
  const byCountry = new Map<string, CountryAuth>();
  const rawSponsorship = collapseSpaces(capInput(profile.requiresSponsorship || "")).trim();
  const statedSponsorship = polarityOf(rawSponsorship);
  // "N/A - citizen" says why no sponsorship applies: unanswered, but not unclear.
  const sponsorshipUnclear = rawSponsorship !== "" && statedSponsorship === null && !NA_BECAUSE_SETTLED.test(rawSponsorship.toLowerCase());
  const reading = readWorkAuth(text);

  if (reading.mentioned.size === 0) {
    // No country anywhere: the statement is about wherever the applicant is
    // (a bare "Yes", "Citizen", "Not authorized").
    const lower = text.toLowerCase();
    // Statuses only from segments with no veto cue ("Not yet a citizen", "I
    // will become a permanent resident": decide-b round 5).
    const all = statusFlagsOf(reading.positiveText);
    const denied =
      /\b(not|no|never)\s+((yet|currently|presently)\s+)?(legally\s+)?(authori[sz]ed|eligible|entitled|permitted|allowed)\b|\bno work (authori[sz]ation|permit)\b/.test(lower) ||
      (!all.pr && /\bnot a (citizen|resident)\b/.test(lower));
    // "No sponsorship needed" is not a No to being authorized. "No, I need
    // sponsorship" is: a leading No that a sponsorship need confirms, with
    // nothing that moves it to later.
    let bareYes = /\bsponsor/.test(lower)
      ? /^(no|nope|n)\b/.test(lower) && SPONSOR_NEED.test(lower) && !SPONSOR_NEGATED.test(lower) && !NOT_TODAY.test(lower)
        ? false
        : null
      : polarityOf(text);
    // A bare Yes next to a vetoed segment ("Yes, once my permit arrives") is
    // not a Yes today. The veto never makes it a No.
    if (bareYes === true && reading.anyVetoed) bareYes = null;
    let unscopedAuthorized: boolean | null = null;
    if (denied || bareYes === false) unscopedAuthorized = false;
    else if (all.affirmative || bareYes === true || all.citizen || all.pr) unscopedAuthorized = true;
    applyExplicitCountries(profile, byCountry);
    return { byCountry, statedSponsorship, unscopedAuthorized, sponsorshipUnclear, withheld: reading.withheld };
  }

  for (const [code, f] of reading.bound) {
    const citizen = f.citizen && !f.notCitizen;
    let auth: CountryAuth | null = null;
    if (f.deny) auth = { authorized: false, needsSponsorship: null, basis: "denied" };
    else if (citizen && !f.pr && !f.student && !f.permit) auth = { authorized: true, needsSponsorship: false, basis: "citizen" };
    else if (f.pr && !f.student && !f.permit) auth = { authorized: true, needsSponsorship: false, basis: "permanent_resident" };
    else if (f.sponsoredVisa) auth = { authorized: true, needsSponsorship: true, basis: "work_permit", employerTied: true };
    else if (f.student) auth = { authorized: null, needsSponsorship: null, basis: "student" };
    else if (f.permit) auth = { authorized: true, needsSponsorship: null, basis: "work_permit" };
    else if (f.affirmative) auth = { authorized: true, needsSponsorship: null, basis: "statement" };
    // "I would require sponsorship to work in the US": sponsorship is needed
    // there; whether they are authorized today is not said.
    // Nor where the answer states citizenship or permanent residence that a
    // veto or the allow-list withheld: what is held there is not known.
    if (f.sponsored && !(auth && SETTLED.has(auth.basis)) && !reading.withheldSettled.includes(code)) auth = { ...(auth ?? { authorized: null, basis: "statement" }), needsSponsorship: true };
    if (auth) byCountry.set(code, auth);
  }
  // "Would need sponsorship" with no country: never where the applicant is a
  // citizen or permanent resident, nor where the answer says so but a veto or
  // the allow-list withheld it ("Canadian citizen - would need sponsorship");
  // otherwise every country they name.
  if (reading.sponsorshipNoCountry) {
    for (const code of reading.mentioned) {
      const auth = byCountry.get(code);
      if (auth && SETTLED.has(auth.basis)) continue;
      if (reading.withheldSettled.includes(code)) continue;
      byCountry.set(code, { ...(auth ?? { authorized: null, basis: "statement" }), needsSponsorship: true });
    }
  }
  applyExplicitCountries(profile, byCountry);
  return { byCountry, statedSponsorship, unscopedAuthorized: null, sponsorshipUnclear, withheld: reading.withheld };
}

/** A status that settles sponsorship for the country (none needed), or a No. */
const SETTLED = new Set<AuthBasis>(["citizen", "permanent_resident", "denied"]);

/** "N/A - citizen", "N/A (US citizen)": not applicable because of a settled status. */
const NA_BECAUSE_SETTLED = /^(?:n ?[/.]? ?a\b\.?|not applicable\b)[^a-z0-9]{0,4}(?:i'?m |i am )?(?:an? )?(?:[a-z]+ )?(?:citizen|permanent resident)\b/;
/** Words that move a leading No to another time ("No, but I will need sponsorship later"). */
const NOT_TODAY = /\b(?:but|later|future|eventually|after|until|yet|currently|now)\b/;

/** Misspellings of the status words, read as the word (decide-b review3: "citizan of canada"). */
const TYPOS: Array<[RegExp, string]> = [
  [/\b(?:citizan|citzen|citizn|citezen|citisen|citizon)(s|ship)?\b/gi, "citizen$1"],
  [/\b(?:canadain|candian|canadien|canadan|canandian)\b/gi, "canadian"],
  [/\b(?:amercian|amerian|amrican)\b/gi, "american"],
  [/\b(?:authroized|autorized|athorized|authorzed|authorizd|authoirzed|authozied|authrized|autherized|authorixed)\b/gi, "authorized"],
  [/\b(?:elligible|eligable|elegible)\b/gi, "eligible"],
  [/\b(?:permenant|permanant|perminant)\b/gi, "permanent"],
];

/** The work-authorization answer as the patterns read it: whitespace runs
 *  collapsed (first, before any other pattern) and status typos corrected. */
function normalizeAnswer(text: string): string {
  let t = collapseSpaces(capInput(text)).trim();
  for (const [re, word] of TYPOS) t = t.replace(re, word);
  return t;
}

/** What the text binds to one country. */
interface Bound {
  citizen: boolean;
  notCitizen: boolean;
  pr: boolean;
  student: boolean;
  sponsoredVisa: boolean;
  permit: boolean;
  affirmative: boolean;
  deny: boolean;
  sponsored: boolean;
}
type BoundKey = keyof Bound;

/** The work-right statuses a piece of text states, with no country. */
interface StatusFlags {
  citizen: boolean;
  pr: boolean;
  student: boolean;
  sponsoredVisa: boolean;
  permit: boolean;
  affirmative: boolean;
}

function statusFlagsOf(t: string): StatusFlags {
  return {
    citizen: /\bcitizen(ship)?\b|\bnational\b/.test(t),
    pr: /\bpermanent resident\b|\bgreen card\b|\blanded immigrant\b|\blawful permanent\b|\bpr\b/.test(t),
    student: /\b(student visa|study permit|f-?1|j-?1|m-?1|international student)\b/.test(t),
    sponsoredVisa: /\b(h-?1b|l-?1|e-?3|o-?1|tn)\b/.test(t),
    permit: /\b(work permit|open work permit|pgwp|post-graduation work permit|ead|employment authori[sz]ation|work visa|opt|cpt)\b/.test(t),
    affirmative: /\b(authori[sz]ed|eligible|entitled|permitted|allowed|legally able|right) to work\b|\bcan (legally )?work\b/.test(t),
  };
}

/**
 * The lowercase text with the periods inside an abbreviation ("u.s.", "u. s.",
 * "u.s.a.", "u.k.") turned into "_", so a period ends a unit only where a
 * sentence does. Same length, so indices still match the original text.
 *
 * The last period of a dotted abbreviation ends a sentence only when a new
 * one starts after it, which the ORIGINAL text shows by a capital: "Not
 * authorized in the U.S. I am a Canadian citizen" ends there; "U.S. citizen"
 * and "U.S. Citizen" do not.
 */
function maskAbbreviations(text: string): string {
  const lower = text.toLowerCase();
  const inner = lower.replace(/\b([a-z])\.(?=\s?[a-z]\.)|\bu\.(?=\s?s\b)/g, (m) => m.replace(".", "_"));
  return inner.replace(/[a-z]_\s?[a-z]\./g, (m, offset: number) => {
    const end = offset + m.length;
    const after = text.slice(end, end + 40);
    const newSentence = /^\s+[A-Z]/.test(after) && !ABBREVIATION_CONTINUES.test(inner.slice(end, end + 40));
    return newSentence ? m : m.slice(0, -1) + "_";
  });
}

/** Words that continue a phrase after "U.S." ("U.S. Citizen", "U.S. Permanent Resident"). */
const ABBREVIATION_CONTINUES =
  /^\s+(citizen\w*|national\w*|resident\w*|permanent|green|work\w*|visa|employment|employer\w*|passport|person|government|based|company|companies)\b/;

/** Every key that names a country: names and aliases of four letters or more,
 *  "us", "usa", "uk", "uae" and "the states", longest first. */
const COUNTRY_KEYS: string[] = (() => {
  const keys = new Set<string>();
  for (const c of COUNTRIES) {
    for (const k of [c.name, ...c.aliases]) {
      const n = geoNorm(k);
      if (n === "states") continue;
      if (n.length >= 4 || n === "us" || n === "usa" || n === "uk" || n === "uae") keys.add(n);
    }
  }
  keys.add("the states");
  return [...keys].sort((a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0));
})();
/** A key as a pattern. "America" is the US only when no continent word
 *  precedes it ("North America", "Latin American"). */
function keySource(k: string): string {
  const src = k.replace(/ /g, "\\s+");
  return k === "america" || k === "american" ? `(?<!north\\s)(?<!south\\s)(?<!latin\\s)(?<!central\\s)${src}` : src;
}
const COUNTRY_ALT = `(?:u_\\s?s(?:_\\s?a)?_?|u_\\s?k_?|${COUNTRY_KEYS.map(keySource).join("|")})`;
/** One country name; LIST is several joined by "and", "or", "&", "/", "+". */
const C = `\\b${COUNTRY_ALT}\\b`;
/** At most eight countries in one list: a longer one would cost O(n^2) as every
 *  later country starts the same scan again. */
const LIST = `${C}(?:\\s*(?:&|\\/|\\+|\\band\\b|\\bor\\b)\\s*(?:the\\s+)?${C}){0,7}`;
const COUNTRY_RE = new RegExp(C, "g");
/** In the text the patterns read, every country name is replaced by a run of
 *  MARK of the same length (so indices still match): the patterns then test
 *  one character class instead of every name, which keeps them linear. */
const MARK = "§";
const M = `${MARK}+`;
/** A list starts at the start of a name: never again inside the same run of
 *  marks (a match from inside would read the same country, one try per letter). */
const MLIST = `(?<!${MARK})${M}(?:\\s*(?:&|\\/|\\+|\\band\\b|\\bor\\b)\\s*(?:the\\s+)?${M}){0,7}`;
/** Demonyms ("canadian"): "not Canadian" is about citizenship, not a work denial. */
const DEMONYMS: Set<string> = (() => {
  const out = new Set<string>(["swiss", "greek", "filipino", "kiwi"]);
  for (const c of COUNTRIES) {
    const name = geoNorm(c.name);
    for (const a of c.aliases) {
      const n = geoNorm(a);
      if (n !== name && /(an|ish|ese|i|ch)$/.test(n)) out.add(n);
    }
  }
  return out;
})();

/** The country a matched key names. */
function codeOfKey(match: string): string | null {
  const key = match.replace(/_/g, " ").replace(/\s+/g, " ").trim();
  if (key === "the states") return "US";
  return countryFromName(key)?.code ?? null;
}

/** Visas and statuses that only exist in one country. */
const VISA =
  "h-?1-?b|tn|l-?1a?|e-?3|o-?1|ead|stem\\s+opt|opt|cpt|f-?1|j-?1|m-?1|green\\s*card|gc|pgwp|post[\\s-]?grad(?:uate|uation)?\\s+work\\s+permit|pr|permanent\\s+residen(?:ce|cy|t)|work\\s+permit|citizenship|refugee\\s+status|protected\\s+person";
/** Needing, applying for, waiting on, being able or planning to get a visa is
 *  not holding it ("need TN", "applying for OPT", "can get a TN", "plan to get
 *  TN", "relocate to the US on a TN"); nor is a visa that is eligible, pending,
 *  in process, in a lottery or starting later ("TN-eligible", "green card -
 *  pending", "green card (pending)", "H-1B lottery", "OPT starting May").
 *  Masked with spaces before statuses are read. The pending word may follow
 *  across one spaced dash, colon or bracket, which would otherwise split it
 *  from its visa into another unit. */
const VISA_INTENT = new RegExp(
  `\\b(?:need|needs|needed|require|requires|required|would\\s+need|will\\s+need|will\\s+get|will\\s+apply\\s+for|apply\\s+for|applying\\s+for|applied\\s+for|waiting\\s+(?:on|for)|awaiting|hoping\\s+(?:to\\s+get|for)|eligible\\s+for|pending` +
    `|(?:can|could|would|might|may)\\s+(?:get|obtain|apply\\s+for|qualify\\s+for)|eligible\\s+to\\s+(?:get|obtain|apply\\s+for)|qualify\\s+for|plan(?:s|ning)?\\s+(?:to\\s+(?:get|obtain|apply\\s+for)|on\\s+(?:getting|applying\\s+for))|intend(?:s|ing)?\\s+to\\s+(?:get|obtain|apply\\s+for)|going\\s+to\\s+(?:get|apply\\s+for))` +
    `\\s+(?:an?\\s+|my\\s+|the\\s+)?(?:(?:the\\s+)?${LIST}\\s+)?(?:${VISA})\\b(?:\\s+(?:visa|status|application|approval|petition))?` +
    `|\\b(?:relocat\\w*|mov(?:e|es|ed|ing))\\s+to\\s+(?:the\\s+)?${LIST}\\s+(?:on|with|via|using|through|under)\\s+(?:an?\\s+|the\\s+)?(?:${VISA})\\b(?:\\s+(?:visa|status))?` +
    `|\\b(?:${VISA})\\b(?:\\s(?:visa|status|application|card))?(?:\\s?[-\\u2013\\u2014:(\\[]\\s?|\\s|-)(?:eligible|eligibility|application|approval|petition|pending|in\\s+progress|in\\s+process|processing|starting|begins?|start\\s+date|lottery|applied|submitted)\\b`,
  "g"
);

function maskVisaIntent(norm: string): string {
  return norm.replace(VISA_INTENT, (m) => " ".repeat(m.length));
}

/** Ends of a unit. The weak ones (comma, spaced dash, brackets, colon) still
 *  let a bare answer and a bare country list pair up ("Yes, Canada"). */
const UNIT_BREAK =
  /\r\n|\n|\r|;|\||\.(?=\s|$)|!|\?|,|\(|\)|\[|\]|\s[-\u2013\u2014]+\s|\s\/\s|\b(?:but|however|whereas|while|although|though)\b|(?<!not )(?<!no )(?<!n't )(?<!n\u2019t )\b(?:now|currently)\b/g;
/** A break that ends a sentence: a sponsorship need said in it stays with the
 *  countries the sentence names. */
const SENTENCE_END = /[\n\r;|.!?]/;

/**
 * The veto (decide-b round 5, D-61 amendment). A segment that holds any cue
 * from backend/data/work_auth_veto.json (someone else, a negation, the past,
 * the future or a condition, a pending or needed status) gets no positive
 * status for any country: no citizen, permanent resident, authorized or visa
 * held. The veto never turns into a No; denials and sponsorship needs are read
 * as before.
 */
function vetoSource(lexicon: Record<string, unknown>): string {
  const entries: string[] = [];
  for (const [family, words] of Object.entries(lexicon)) {
    if (family.startsWith("_") || !Array.isArray(words)) continue;
    for (const w of words) entries.push(String(w).toLowerCase());
  }
  const unique = [...new Set(entries)].sort((a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0));
  const alts = unique.map((w) => {
    const body = w.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&").replace(/ /g, "\\s");
    const lead = /^[a-z0-9\u00e0-\u00ff]/.test(w) ? "(?<![a-z0-9\u00e0-\u00ff])" : "";
    const trail = /[a-z0-9\u00e0-\u00ff]$/.test(w) ? "(?![a-z0-9\u00e0-\u00ff])" : "";
    return lead + body + trail;
  });
  return alts.join("|");
}
const VETO = new RegExp(vetoSource(VETO_LEXICON as Record<string, unknown>));
/** A negated sponsorship need ("do not require sponsorship", "no sponsorship
 *  needed", "without sponsorship"): a sponsorship fact of its own, read
 *  elsewhere, so its "not" or "need" vetoes nothing beside it. */
const NEGATED_SPONSOR_PHRASE = new RegExp(
  "(?:\\bnot|n't|\\bnever|\\bno\\s+longer)\\s+(?:\\w+\\s+){0,2}?(?:need|require)(?:s|d|ing)?\\s+(?:any\\s+|a\\s+|an\\s+)?(?:(?:visa|work|employer|employment|immigration)\\s+)?(?:sponsorship|sponsor|visa)\\b" +
    "|\\b(?:no|without|not\\s+requiring|not\\s+needing)\\s+(?:any\\s+|a\\s+)?(?:need\\s+(?:for|of)\\s+(?:any\\s+)?)?(?:(?:visa|work|employer|employment|immigration)\\s+)?(?:sponsorship|sponsor|visa)(?:\\s+(?:is\\s+)?(?:needed|required|necessary))?\\b" +
    "|\\b(?:sponsorship|sponsor|visa)\\s+(?:is\\s+not|isn't|not|never)\\s+(?:needed|required|necessary)\\b",
  "g"
);
/** Does this piece of the lowercased text hold a veto cue? */
function vetoed(lowerUnit: string): boolean {
  return VETO.test(lowerUnit.replace(/\u2019/g, "'").replace(NEGATED_SPONSOR_PHRASE, " "));
}
/**
 * The allow-list layer (decide-b, D-61 amendment: positives only from a fully
 * recognised answer). Every term of backend/data/work_auth_allowlist.json and
 * every country key, mapped to its family ("status", "country", ...).
 */
const ALLOW_FAMILY: Map<string, string> = (() => {
  const out = new Map<string, string>();
  const lists = ALLOW_LIST as Record<string, unknown>;
  const suffixes = (lists.sponsorship_suffixes as string[]) ?? [];
  for (const [family, words] of Object.entries(lists)) {
    if (family.startsWith("_") || family === "sponsorship_suffixes" || !Array.isArray(words)) continue;
    for (const w of words) {
      const term = String(w).toLowerCase();
      if (!out.has(term)) out.set(term, family);
      if (family === "sponsorship") for (const s of suffixes) if (!out.has(`${term} ${s}`)) out.set(`${term} ${s}`, family);
    }
  }
  for (const k of COUNTRY_KEYS) if (!out.has(k)) out.set(k, "country");
  return out;
})();
const escapeTerm = (t: string): string => t.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&").replace(/ /g, "\\s");
/** Longest first, so the alternation takes the longest term that fits. */
const byLength = (a: string, b: string): number => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0);
/** One token: a term of several words or with punctuation ("permanent
 *  resident", "h-1b", "u.s."), else a word, else one other character. A term
 *  never ends inside a word ("us" in "usual", "canada" in "canada's"). */
const ALLOW_TOKEN = new RegExp(
  `(${[...ALLOW_FAMILY.keys()].filter((k) => /[^a-z0-9']/.test(k)).sort(byLength).map(escapeTerm).join("|")})(?![a-z0-9]|'[a-z])|([a-z][a-z0-9']*)|[\\s\\S]`,
  "g"
);
const COUNTRY_TERM = [...ALLOW_FAMILY.entries()].filter(([, f]) => f === "country").map(([k]) => k).sort(byLength).map(escapeTerm).join("|");
/** "Canada:", "US / Canada:", "the U.S.:", "Canada -": the label of a labelled line. */
const ALLOW_LABEL = new RegExp(`^\\s?(?:the\\s)?(?:${COUNTRY_TERM})(?:\\s?(?:&|\\/|\\+|and)\\s?(?:the\\s)?(?:${COUNTRY_TERM})){0,7}\\s?(?::|-|\\u2013|\\u2014)`);
/** A letter or mark outside ASCII, or an invisible format character: never in
 *  the vocabulary. Every Cyrillic or Greek lookalike of a Latin letter is one. */
const NON_ASCII_LETTER = /(?![\x00-\x7f])[\p{L}\p{M}\p{Cf}]/u;

/** The answer as the vocabulary reads it: NFKC, whitespace collapsed, ASCII
 *  letters lowercased (with no other letter left, that is the casefold), curly
 *  apostrophes straight. Null when a non-ASCII letter survives NFKC. */
function foldForAllowList(text: string): string | null {
  const t = collapseSpaces(text.normalize("NFKC")).trim();
  if (NON_ASCII_LETTER.test(t)) return null;
  return t.replace(/[A-Z]/g, (c) => c.toLowerCase()).replace(/[\u2018\u2019]/g, "'");
}

/**
 * Is the answer outside the closed vocabulary, for a positive status? One word
 * that is not listed or a non-ASCII letter is enough. So is a negative answer
 * word ("US citizen? No"), "my" before anything but a status or a visa
 * (country names may stand between: "my Canadian citizenship"), and "on"
 * before anything but a visa, "a" or "an" aside ("On OPT", never "working on
 * my green card").
 */
export function outOfVocabulary(text: string): boolean {
  const t = foldForAllowList(text);
  if (t === null) return true;
  const tokens: Array<[string, string]> = [];
  for (const m of t.matchAll(ALLOW_TOKEN)) {
    const term = m[1] !== undefined ? m[1].replace(/\s+/g, " ") : m[2];
    if (term === undefined) continue;
    const family = ALLOW_FAMILY.get(term);
    if (family === undefined || family === "negative_answer") return true;
    tokens.push([term, family]);
  }
  const familyAt = (j: number): string => tokens[j]?.[1] ?? "";
  for (let i = 0; i < tokens.length; i++) {
    let j = i + 1;
    if (tokens[i][1] === "possessive") {
      while (familyAt(j) === "country") j++;
      if (familyAt(j) !== "status" && familyAt(j) !== "visa") return true;
    } else if (tokens[i][1] === "visa_preposition") {
      if (tokens[j]?.[0] === "a" || tokens[j]?.[0] === "an") j++;
      if (familyAt(j) !== "visa") return true;
    }
  }
  return false;
}

/** The ranges of the answer that may bind no positive status: the whole answer
 *  when anything in it is outside the vocabulary, or, in a labelled multi-line
 *  answer ("Canada: citizen\nUS: no"), each line that is. */
function outOfVocabularyRanges(text: string): Array<[number, number]> {
  const lines: Array<[number, number]> = [];
  let start = 0;
  for (let i = 0; i <= text.length; i++) {
    if (i < text.length && text[i] !== "\n") continue;
    if (text.slice(start, i).trim()) lines.push([start, i]);
    start = i + 1;
  }
  const labelled = lines.length >= 2 && lines.every(([a, b]) => ALLOW_LABEL.test(foldForAllowList(text.slice(a, b)) ?? ""));
  if (labelled) return lines.filter(([a, b]) => outOfVocabulary(text.slice(a, b)));
  return outOfVocabulary(text) ? [[0, text.length]] : [];
}

/** The keys that assign a positive status: the veto blocks only these. */
const POSITIVE = new Set<BoundKey>(["citizen", "pr", "student", "sponsoredVisa", "permit", "affirmative"]);
const WEAK_BREAK = /^(?:,|\(|\)|\[|\]|\s[-\u2013\u2014]+\s)$/;
const ANSWER_WORD = /^\s*(?:(yes|y|yeah|yep|oui|si|[\u2705\u2714\u2611\u2713])|(no|n|nope|non|[\u274c\u2717\u2718\u2716]))\s*$/;
const PURE_LIST = new RegExp(`^\\s*(?:(?:only\\s+)?(?:in|for)\\s+)?(?:the\\s+)?(${MLIST})\\s*(?:only\\s*)?(?::\\s*)?$`, "d");
const STATUS_ONLY_CITIZEN = /\b(?:dual\s+)?citizen\w*\b|\bnationals?\b/;
const STATUS_ONLY_PR = /\bpermanent\s+resident\w*|\bperm(?:anent)?\.?\s+res\w*|\bpr\b/;
const STATUS_ONLY_PERMIT = /\b(?:open\s+)?work\s+(?:permit|visa)\b/;
const SPONSOR_NEED = /\b(?:need|needs|require|requires|requiring|would\s+need|will\s+need|would\s+require|will\s+require)\s+(?:a\s+|an\s+)?(?:visa\s+|work\s+)?(?:sponsorship|sponsor|visa)\b/;
const SPONSOR_NEGATED =
  /\b(?:not|no|never|without|do\s+not|does\s+not|will\s+not|don['\u2019]?t|doesn['\u2019]?t|won['\u2019]?t)\s+(?:\w+\s+){0,2}?(?:need|require|requiring|sponsor)/;

/** Global, with the start and end of every group (the list's countries). */
const R = (src: string): RegExp => new RegExp(src, "gd");
const MARK_RUN = new RegExp(M, "g");
const YES_MARK = "[\\u2705\\u2714\\u2611\\u2713]";
const NO_MARK = "[\\u274c\\u2717\\u2718\\u2716]";
const AUTH_ADJ = "(?:authori[sz]ed|eligible|entitled|permitted|allowed|legally\\s+able|able|free|cleared)";
const AUTH_NOUN = "(?:authori[sz]ation|auth|permit|status|visa|eligibility)";

/** Patterns, in the order they are read. `L` is the list group; `kind` what it binds. */
const POSTFIX_PATTERNS: Array<[RegExp, BoundKey]> = [
  // "Canada: yes", "Canada yes", "US work authorization: no", "US \u274c", "US not authorized"
  [R(`(${MLIST})\\s*(?:work\\s+(?:authori[sz]ation|auth|permit)\\s*)?(?::|-|\\u2013)?\\s*(?:\\b(?:yes|oui)\\b|${YES_MARK})`), "affirmative"],
  [R(`(${MLIST})\\s*(?:work\\s+(?:authori[sz]ation|auth|permit)\\s*)?(?::|-|\\u2013)?\\s*(?:\\bno\\b|\\bnone\\b(?!\\s(?:needed|required|necessary))|\\bnot\\s+(?:(?:yet|currently)\\s+)?(?:legally\\s+)?(?:authori[sz]ed|eligible|allowed|permitted)\\b|${NO_MARK})`), "deny"],
];
const PREFIX_ANSWER_PATTERNS: Array<[RegExp, BoundKey]> = [
  // "Yes for Canada", "\u2705 Canadian citizen", "no for US", "\u274c US"
  [R(`(?:\\b(?:yes|oui)\\b|${YES_MARK})\\s*(?:[:\\-\\u2013]\\s*)?(?:(?:for|in)\\s+)?(?:the\\s+)?(${MLIST})`), "affirmative"],
  [R(`(?:\\bno\\b|${NO_MARK})\\s*(?:[:\\-\\u2013]\\s*)?(?:(?:for|in)\\s+)?(?:the\\s+)?(${MLIST})`), "deny"],
];
const PATTERNS: Array<[RegExp, BoundKey]> = [
  // Denials.
  [R(`\\b(?:not|never)\\s+(?:(?:yet|currently|presently)\\s+)?(?:legally\\s+)?${AUTH_ADJ}\\s+(?:\\(?yet\\)?\\s+)?(?:to\\s+(?:legally\\s+)?work\\s+)?(?:(?:in|for|within)\\s+)?(?:the\\s+)?(${MLIST})`), "deny"],
  [R(`\\b(?:except|excluding|other\\s+than|outside(?:\\s+of)?)\\s+(?:for\\s+)?(?:in\\s+)?(?:the\\s+)?(${MLIST})`), "deny"],
  [R(`(?:\\bcannot|\\bcan['\\u2019]?t|\\bcan\\s+not|\\bunable\\s+to|\\bnot\\s+able\\s+to)\\s+(?:legally\\s+)?work\\s+(?:in|for)\\s+(?:the\\s+)?(${MLIST})`), "deny"],
  [R(`\\b(?:ineligible|unauthori[sz]ed)\\s+(?:to\\s+work\\s+)?(?:(?:in|for)\\s+)?(?:the\\s+)?(${MLIST})`), "deny"],
  [R(`(?:\\bno|\\bwithout|\\b(?:do|does)\\s+not\\s+(?:currently\\s+|yet\\s+)?have|\\b(?:don|doesn)['\\u2019]?t\\s+(?:currently\\s+|yet\\s+)?have|${NO_MARK})\\s*(?:an?\\s+|any\\s+)?(?:the\\s+)?(${MLIST})\\s+(?:work\\s+)?${AUTH_NOUN}`), "deny"],
  [R(`(?:\\bno|\\bwithout|\\b(?:do|does)\\s+not\\s+(?:currently\\s+|yet\\s+)?have|\\b(?:don|doesn)['\\u2019]?t\\s+(?:currently\\s+|yet\\s+)?have)\\s+(?:an?\\s+|any\\s+)?(?:work\\s+)?(?:authori[sz]ation|permit|permission|right|status)\\s+(?:to\\s+work\\s+)?(?:in|for)\\s+(?:the\\s+)?(${MLIST})`), "deny"],
  // "not a citizen of the US", "US citizen not"
  [R(`\\bnot\\s+(?:an?\\s+)?(?:citizen\\w*|nationals?|resident\\w*)\\s+of\\s+(?:the\\s+)?(${MLIST})`), "notCitizen"],
  [R(`(${MLIST})\\s+citizen\\w*\\s+(?:not|no)\\s*$`), "notCitizen"],
  // Citizenship: "US citizen", "Canada: citizen", "citizen of (both) Canada and the US".
  [R(`(${MLIST})\\s+(?:dual\\s+)?(?:citizen\\w*|nationals?)\\b`), "citizen"],
  [R(`(${MLIST})\\s?:\\s?(?:dual\\s+)?(?:citizen|citizenship|national)\\b(?!\\s(?:of|in)\\b)`), "citizen"],
  [R(`\\b(?:dual\\s+)?(?:citizen\\w*|nationals?)\\s*(?:of|:|-|\\u2013)\\s*(?:both\\s+)?(?:the\\s+)?(${MLIST})`), "citizen"],
  // "I hold dual citizenship with the United Kingdom and Canada".
  [R(`\\bdual\\s+citizenship\\s+with\\s+(?:both\\s+)?(?:the\\s+)?(${MLIST})`), "citizen"],
  // Permanent residence: "US permanent resident", "Canada: PR", "PR of Canada".
  [R(`(${MLIST})\\s+(?:permanent\\s+resident\\w*|perm(?:anent)?\\.?\\s+res(?:ident)?\\w*|pr|lpr|landed\\s+immigrant)\\b`), "pr"],
  [R(`(${MLIST})\\s?:\\s?(?:permanent\\s+resident|pr|lpr|landed\\s+immigrant)\\b(?!\\s(?:of|in)\\b)`), "pr"],
  [R(`\\b(?:permanent\\s+resident\\w*|perm(?:anent)?\\.?\\s+res(?:ident)?\\w*|pr|lpr)\\s*(?:of|in|:|-|\\u2013)\\s*(?:the\\s+)?(${MLIST})`), "pr"],
  // Authorized to work there.
  [R(`\\b${AUTH_ADJ}\\s+(?:to\\s+(?:legally\\s+)?work\\s+)?(?:only\\s+)?(?:in|for|within)\\s+(?:both\\s+)?(?:the\\s+)?(${MLIST})`), "affirmative"],
  [R(`\\bcan\\s+(?:legally\\s+)?work\\s+(?:only\\s+)?(?:in|for)\\s+(?:both\\s+)?(?:the\\s+)?(${MLIST})`), "affirmative"],
  [R(`\\bright\\s+to\\s+work\\s+in\\s+(?:the\\s+)?(${MLIST})`), "affirmative"],
  [R(`\\bonly\\s+(?:(?:in|for)\\s+)?(?:the\\s+)?(${MLIST})`), "affirmative"],
  [R(`(${MLIST})\\s+only\\b`), "affirmative"],
  // A permit or student visa named for a country.
  [R(`(${MLIST})\\s+(?:open\\s+)?(?:work\\s+permit|work\\s+visa)`), "permit"],
  [R(`(${MLIST})\\s?:\\s?(?:an?\\s)?(?:open\\s+)?(?:work\\s+permit|work\\s+visa)`), "permit"],
  [R(`\\b(?:open\\s+)?(?:work\\s+permit|work\\s+visa)\\s*(?:in|for|of|:|-)\\s*(?:the\\s+)?(${MLIST})`), "permit"],
  // "Refugee status in Canada", "protected person in Canada": allowed to work.
  [R(`\\b(?:refugee\\s+status|protected\\s+person(?:\\s+status)?)\\s+(?:in|of)\\s+(?:the\\s+)?(${MLIST})`), "permit"],
  [R(`(${MLIST})\\s+(?:student\\s+visa|study\\s+permit)`), "student"],
  [R(`(${MLIST})\\s?:\\s?(?:an?\\s)?(?:student\\s+visa|study\\s+permit|international\\s+student|student)\\b`), "student"],
  [R(`\\b(?:student\\s+visa|international\\s+student)\\s+(?:in|for)\\s+(?:the\\s+)?(${MLIST})`), "student"],
  // Sponsorship needed there: "need sponsorship for the US", "need an LMIA to
  // work in Canada", "US requires sponsorship", "US: need visa", "USA:
  // sponsorship required".
  [R(`\\b(?:would\\s+need|will\\s+need|would\\s+require|will\\s+require|need|needs|needed|require|requires|required|requiring)\\s+(?:a\\s+|an\\s+)?(?:new\\s+)?(?:visa\\s+|work\\s+)?(?:sponsorship|sponsor|visa|lmia)\\s+(?:to\\s+work\\s+)?(?:for|in)\\s+(?:the\\s+)?(${MLIST})`), "sponsored"],
  [R(`(${MLIST})(?:\\s?:)?\\s(?:\\w+\\s)?(?:requires?|needs?|requiring)\\s(?:a\\s|an\\s)?(?:visa\\s)?(?:sponsorship|visa)`), "sponsored"],
  [R(`(${MLIST})(?:\\s?:)?\\s(?:visa\\s)?sponsorship\\s(?:is\\s)?(?:required|needed)\\b`), "sponsored"],
];
/** Bound to Canada wherever it is said: an LMIA exists only there. */
const LMIA_NEED = /\b(?:need|needs|needed|require|requires|required|requiring)\s(?:an?\s)?(?:new\s)?lmia\b/;
/** Needing an employer-sponsored US visa ("Need H-1B sponsorship", "will need
 *  H-1B after"): read before the visa itself is masked as not held. */
const SPONSORED_VISA_NEED =
  /\b(?:need|needs|needed|require|requires|required|requiring|would\s(?:need|require)|will\s(?:need|require))\s(?:an?\s|the\s|my\s)?(?:new\s)?(?:h-?1-?b|l-?1a?|e-?3|o-?1)\b/;
/** A unit that is one demonym and nothing that changes it: "Canadian", "I'm
 *  Canadian", "American living in Toronto". */
const DEMONYM_UNIT = new RegExp(`^\\s?(?:i['\\u2019]?m\\s|im\\s|i\\sam\\s)?(?:an?\\s)?(${M})\\s?(?:$|(?:living|residing|based|who)\\b)`, "d");
/** A status-only unit in brackets after a single country: "Authorized to work
 *  in Canada (citizen)", "Canada: yes (citizen)". */
const SINGLE_COUNTRY_AT_END = new RegExp(`(${MLIST})\\s?(?::\\s?(?:yes|oui)\\s?)?$`, "d");
/** A unit that is only a denial: "not yet authorized". */
const DENIAL_ONLY = /^\s?not\s(?:(?:yet|currently)\s)?(?:legally\s)?(?:authori[sz]ed|eligible|allowed|permitted)(?:\sto\swork)?\s?$/;
/** Intent or plan words: a permit "with" them is not one held. */
const INTENT_WORDS = /\b(?:plan\w*|hop(?:e|es|ing)|want\w*|would|will|intend\w*|looking|seek(?:s|ing)?|wish(?:es|ing)?|apply(?:ing)?)\b/;
/** A letter or an answer mark: a unit without one binds nothing to its countries. */
const WORD_OR_MARK = /[a-z✅✔☑✓❌✗✘✖]/;
/** "in Canada with an open work permit". */
const PERMIT_WITH = new RegExp(`\\bin\\s(?:the\\s)?(${MLIST})\\swith\\s(?:an?\\s)?(?:open\\s)?work\\s(?:permit|visa)\\b`, "gd");
/** "not in the US", "not the U.S.", "not a US citizen", "not Canadian". */
const NOT_PLACE = R(`\\b(?:not|never)\\s+(?:(?:yet|currently|presently)\\s+)?(?:(?:in|for|within)\\s+)?(an?\\s+)?(?:the\\s+)?(${MLIST})(\\s+(?:citizen\\w*|nationals?|person|passport|resident\\w*))?`);
/** Statuses that belong to one country whatever the text says around them. */
const INHERENT: Array<[RegExp, string, BoundKey]> = [
  [/\b(?:h-?1-?b|tn|l-?1a?|e-?3|o-?1)\b/g, "US", "sponsoredVisa"],
  [/\b(?:ead|stem\s+opt|opt|cpt)\b/g, "US", "permit"],
  [/\b(?:f-?1|j-?1|m-?1)\b/g, "US", "student"],
  [/\b(?:green\s*card|gc|lpr)\b/g, "US", "pr"],
  [/\b(?:pgwp|post[\s-]?grad(?:uate|uation)?\s+work\s+permit)\b/g, "CA", "permit"],
  [/\bstudy\s+permit\b/g, "CA", "student"],
  [/\b(?:pr\s+card|landed\s+immigrant)\b/g, "CA", "pr"],
];

interface Unit {
  start: number;
  end: number;
  /** The break after this unit up to the next one. */
  breakAfter: string;
}

interface Reading {
  /** Every country the text binds a status, a Yes or a No to. */
  bound: Map<string, Bound>;
  /** Every country the text names at all (bound or not). */
  mentioned: Set<string>;
  /** "Would need sponsorship" said with no country. */
  sponsorshipNoCountry: boolean;
  /** The masked text with every vetoed segment blanked: what a status may be
   *  read from when no country is named. */
  positiveText: string;
  /** Some segment held a veto cue. */
  anyVetoed: boolean;
  /** Countries a veto kept a positive status from, in the order met. */
  withheld: string[];
  /** Of those, the countries whose withheld status was citizenship or
   *  permanent residence. */
  withheldSettled: string[];
}

const emptyBound = (): Bound => ({ citizen: false, notCitizen: false, pr: false, student: false, sponsoredVisa: false, permit: false, affirmative: false, deny: false, sponsored: false });

/**
 * Read a free-text work authorization the binding way (decide-b round 3): a
 * country gets a status, a Yes or a No only from a pattern bound directly to
 * it ("US citizen", "citizen of Canada", "authorized to work in the US",
 * "US: no", "Yes for Canada", "green card"). A country the text only names
 * ("living in Canada", "US-based") gets nothing, so it stays unknown. The
 * text splits into units at line breaks, ";", "|", " / ", sentence ends,
 * commas, brackets, spaced dashes and "but"; a bare answer and a bare country
 * list next to each other ("Yes, Canada", "Canada - Yes", "PR (Canada)") pair.
 */
function readWorkAuth(text: string): Reading {
  const bound = new Map<string, Bound>();
  const mentioned = new Set<string>();
  let sponsorshipNoCountry = false;
  /** Units holding a veto cue: they bind no positive status (decide-b round 5). */
  const vetoedUnit = new Set<number>();
  const withheld: string[] = [];
  const withheldSettled: string[] = [];
  const bindIn = (unitIdx: number[], codes: string[], key: BoundKey): void => {
    if (POSITIVE.has(key) && unitIdx.some((i) => vetoedUnit.has(i))) {
      for (const code of codes) if (!withheld.includes(code)) withheld.push(code);
      if (key === "citizen" || key === "pr") for (const code of codes) if (!withheldSettled.includes(code)) withheldSettled.push(code);
      return;
    }
    bind(codes, key);
  };
  const bind = (codes: string[], key: BoundKey): void => {
    for (const code of codes) {
      const b = bound.get(code) ?? emptyBound();
      b[key] = true;
      bound.set(code, b);
      mentioned.add(code);
    }
  };
  const unmasked = maskAbbreviations(text);
  const masked = maskVisaIntent(unmasked);

  // Every country named. The patterns read `norm`, where each name is a run
  // of MARK; `names` maps the run's start to the country and its key. A
  // lowercase "us" counts as named only where it reads as the country ("in
  // us", "us citizen", "us: no"), never "work for us"; the patterns still see
  // it, and only bind it where a country belongs.
  const names = new Map<number, { code: string; key: string }>();
  /** Starts of the names that count as named (not the pronoun "us"). */
  const named = new Set<number>();
  const pieces: string[] = [];
  let last = 0;
  for (const m of masked.matchAll(COUNTRY_RE)) {
    const at = m.index ?? 0;
    const code = codeOfKey(m[0]);
    if (!code) continue;
    names.set(at, { code, key: m[0].replace(/\s+/g, " ") });
    pieces.push(masked.slice(last, at), MARK.repeat(m[0].length));
    last = at + m[0].length;
    if (m[0] === "us" && text.slice(at, at + 2) !== "US" && !LOWERCASE_US_BEFORE.test(masked.slice(Math.max(0, at - 8), at)) && !LOWERCASE_US_AFTER.test(masked.slice(at + 2, at + 24))) continue;
    named.add(at);
    mentioned.add(code);
  }
  pieces.push(masked.slice(last));
  const norm = pieces.join("");
  const codesAt = (start: number, end: number): string[] => {
    const out: string[] = [];
    for (const m of norm.slice(start, end).matchAll(MARK_RUN)) {
      const name = names.get(start + (m.index ?? 0));
      if (name && !out.includes(name.code)) out.push(name.code);
    }
    return out;
  };
  /** The countries named (never the pronoun "us") between two indices. */
  const namedAt = (start: number, end: number): string[] => {
    const out: string[] = [];
    for (const m of norm.slice(start, end).matchAll(MARK_RUN)) {
      const at = start + (m.index ?? 0);
      const name = names.get(at);
      if (name && named.has(at) && !out.includes(name.code)) out.push(name.code);
    }
    return out;
  };

  // Units.
  const units: Unit[] = [];
  let at = 0;
  let pending = "";
  for (const m of norm.matchAll(UNIT_BREAK)) {
    const idx = m.index ?? 0;
    if (norm.slice(at, idx).trim()) {
      if (units.length) units[units.length - 1].breakAfter = pending;
      units.push({ start: at, end: idx, breakAfter: "" });
      pending = "";
    }
    pending += m[0];
    at = idx + m[0].length;
  }
  if (norm.slice(at).trim()) {
    if (units.length) units[units.length - 1].breakAfter = pending;
    units.push({ start: at, end: norm.length, breakAfter: "" });
  }
  // The veto, read on the text before any visa or country is masked.
  units.forEach((u, i) => {
    if (vetoed(unmasked.slice(u.start, u.end))) vetoedUnit.add(i);
  });
  // The allow-list layer: a unit in a range with any word outside the closed
  // vocabulary binds no positive status either (decide-b, D-61 amendment).
  const outside = outOfVocabularyRanges(text);
  units.forEach((u, i) => {
    if (outside.some(([a, b]) => u.start < b && a < u.end)) vetoedUnit.add(i);
  });
  // Statuses that belong to one country, from each unit that has no veto cue.
  units.forEach((u, i) => {
    const piece = masked.slice(u.start, u.end);
    for (const [re, code, key] of INHERENT) {
      if (piece.search(re) >= 0) bindIn([i], [code], key);
    }
  });
  // Sentences: a need said in a unit with no country belongs to the countries
  // its sentence names (decide-b round 5: a need for one country stays there).
  const sentenceOf: number[] = [];
  const sentenceCodes: string[][] = [];
  units.forEach((u, i) => {
    const s = i === 0 ? 0 : SENTENCE_END.test(units[i - 1].breakAfter) ? sentenceOf[i - 1] + 1 : sentenceOf[i - 1];
    sentenceOf.push(s);
    if (!sentenceCodes[s]) sentenceCodes[s] = [];
    for (const code of namedAt(u.start, u.end)) if (!sentenceCodes[s].includes(code)) sentenceCodes[s].push(code);
  });
  const needInSentence = (i: number): void => {
    const codes = sentenceCodes[sentenceOf[i]] ?? [];
    if (codes.length) bind(codes, "sponsored");
    else sponsorshipNoCountry = true;
  };

  // Bare answers next to bare country lists ("Yes, Canada", "Canada - Yes").
  const paired = new Set<number>();
  const answerOf = (u: Unit): BoundKey | null => {
    const m = ANSWER_WORD.exec(norm.slice(u.start, u.end));
    return m ? (m[1] ? "affirmative" : "deny") : null;
  };
  const statusOnly = (u: Unit): BoundKey[] => {
    const s = norm.slice(u.start, u.end);
    if (s.includes(MARK)) return [];
    const keys: BoundKey[] = [];
    if (STATUS_ONLY_CITIZEN.test(s)) keys.push("citizen");
    if (STATUS_ONLY_PR.test(s)) keys.push("pr");
    if (STATUS_ONLY_PERMIT.test(s)) keys.push("permit");
    if (SPONSOR_NEED.test(s) && !SPONSOR_NEGATED.test(s)) keys.push("sponsored");
    if (DENIAL_ONLY.test(s)) keys.push("deny");
    return keys;
  };
  const listOf = (u: Unit): string[] => {
    const m = PURE_LIST.exec(norm.slice(u.start, u.end));
    return m ? codesAt(u.start + m.indices![1][0], u.start + m.indices![1][1]) : [];
  };
  for (let i = 0; i + 1 < units.length; i++) {
    if (paired.has(i) || !WEAK_BREAK.test(units[i].breakAfter)) continue;
    const a = units[i];
    const b = units[i + 1];
    const tryPair = (list: Unit, other: Unit): boolean => {
      const codes = listOf(list);
      if (!codes.length) return false;
      const answer = answerOf(other);
      const keys = answer ? [answer] : statusOnly(other);
      if (!keys.length) return false;
      for (const k of keys) bindIn([i, i + 1], codes, k);
      return true;
    };
    if (tryPair(b, a) || tryPair(a, b)) {
      paired.add(i);
      paired.add(i + 1);
    }
  }
  // A status-only unit in brackets after one country: "Authorized to work in
  // Canada (citizen)", "Canada: yes (citizen)". A list before it ("Canada and
  // the US (citizen)") is ambiguous and binds nothing.
  for (let i = 1; i < units.length; i++) {
    if (paired.has(i) || units[i - 1].breakAfter.trim() !== "(" || !/^\s?\)/.test(units[i].breakAfter)) continue;
    const keys = statusOnly(units[i]).filter((k) => k === "citizen" || k === "pr" || k === "permit");
    if (!keys.length) continue;
    const prev = units[i - 1];
    const m = SINGLE_COUNTRY_AT_END.exec(norm.slice(prev.start, prev.end));
    if (!m) continue;
    const codes = codesAt(prev.start + m.indices![1][0], prev.start + m.indices![1][1]);
    if (codes.length !== 1) continue;
    for (const k of keys) bindIn([i - 1, i], codes, k);
    paired.add(i);
  }

  units.forEach((u, i) => {
    if (paired.has(i)) return;
    const s = norm.slice(u.start, u.end);
    const before = unmasked.slice(u.start, u.end);
    // Needs that bind a country of their own, said with no country.
    const negatedNeed = SPONSOR_NEGATED.test(before);
    if (!negatedNeed && LMIA_NEED.test(before)) bind(["CA"], "sponsored");
    if (!negatedNeed && SPONSORED_VISA_NEED.test(before)) bind(["US"], "sponsored");
    // A unit with no country binds nothing more: only a sponsorship need
    // said with no country is left to read.
    if (!s.includes(MARK)) {
      if (!SPONSOR_NEGATED.test(s) && SPONSOR_NEED.test(s)) needInSentence(i);
      return;
    }
    // "Canadian", "I'm Canadian", "American living in Toronto".
    const dm = DEMONYM_UNIT.exec(s);
    if (dm) {
      const name = names.get(u.start + dm.indices![1][0]);
      if (name && DEMONYMS.has(name.key) && name.key.length === dm[1].length) bindIn([i], [name.code], "citizen");
    }
    // Countries and punctuation alone ("Canada", "US: -"): every pattern
    // below needs a word or an answer mark beside the country.
    if (!WORD_OR_MARK.test(s.replace(MARK_RUN, ""))) return;
    if (!INTENT_WORDS.test(s)) {
      for (const m of s.matchAll(PERMIT_WITH)) bindIn([i], codesAt(u.start + m.indices![1][0], u.start + m.indices![1][1]), "permit");
    }
    const listCodes = (m: RegExpMatchArray, group: number): string[] => codesAt(u.start + m.indices![group][0], u.start + m.indices![group][1]);
    // "Authorized to work in the US? No.": the answer flips the question.
    const next = units[i + 1];
    const answeredNo = /\?/.test(u.breakAfter) && next !== undefined && answerOf(next) === "deny";
    const used: Array<[number, number]> = [];
    const overlaps = (x: number, y: number): boolean => used.some(([p, q]) => x < q && p < y);
    for (const [re, key] of POSTFIX_PATTERNS) {
      for (const m of s.matchAll(re)) {
        const end = (m.index ?? 0) + m[0].length;
        used.push([(m.index ?? 0) + m[1].length, end]);
        bindIn([i], listCodes(m, 1), key);
      }
    }
    for (const [re, key] of PREFIX_ANSWER_PATTERNS) {
      for (const m of s.matchAll(re)) {
        const start = m.index ?? 0;
        const answerEnd = start + m[0].length - m[1].length;
        if (overlaps(start, answerEnd)) continue;
        bindIn([i], listCodes(m, 1), key);
      }
    }
    const negatedSponsor = SPONSOR_NEGATED.test(s);
    let sponsorBound = false;
    for (const [re, key] of PATTERNS) {
      if (key === "sponsored" && negatedSponsor) continue;
      for (const m of s.matchAll(re)) {
        // "PR in Canada", but not "US permanent resident in Canada": the
        // status already has its country before it.
        if (key === "pr" && PR_AFTER.test(m[0]) && LIST_AT_END.test(s.slice(Math.max(0, (m.index ?? 0) - 40), m.index ?? 0))) continue;
        const flipped: BoundKey = answeredNo && key === "affirmative" ? "deny" : key;
        if (key === "sponsored") sponsorBound = true;
        bindIn([i], listCodes(m, 1), flipped);
      }
    }
    for (const m of s.matchAll(NOT_PLACE)) {
      const codes = listCodes(m, 2);
      const first = names.get(u.start + m.indices![2][0])?.key ?? "";
      const aboutCitizenship = Boolean(m[1]) || Boolean(m[3]) || DEMONYMS.has(first);
      bind(codes, aboutCitizenship ? "notCitizen" : "deny");
    }
    // A need bound to no country by a pattern: the countries this unit names,
    // else its sentence's (never every country the answer names).
    if (!sponsorBound && !negatedSponsor && SPONSOR_NEED.test(s)) {
      const own = namedAt(u.start, u.end);
      if (own.length) bind(own, "sponsored");
      else needInSentence(i);
    }
  });
  let positiveText = "";
  let at2 = 0;
  units.forEach((u, i) => {
    if (!vetoedUnit.has(i)) return;
    positiveText += masked.slice(at2, u.start) + " ".repeat(u.end - u.start);
    at2 = u.end;
  });
  positiveText += masked.slice(at2);
  return { bound, mentioned, sponsorshipNoCountry, positiveText, anyVetoed: vetoedUnit.size > 0, withheld, withheldSettled };
}

/** The "PR in/of <country>" pattern, and a country right before a match. */
const PR_AFTER = /^(?:permanent|perm|pr|lpr)/;
const LIST_AT_END = new RegExp(`${M}\\s+$`);
/** Before a lowercase "us" that is the country: "in us", "for the us". */
const LOWERCASE_US_BEFORE = /\b(?:in|for|the|to|from|of)\s+$/;
/** After a lowercase "us" that is the country: "us citizen", "us: no", "us-based". */
const LOWERCASE_US_AFTER =
  /^\s*(?:citizen|national|pr\b|permanent|green|gc\b|person|work|employment|jobs?\b|roles?\b|visa|passport|based|-|resident|:|yes\b|no\b|only\b|requires?\b|needs?\b|[\u2705\u2714\u2611\u2713\u274c\u2717\u2718\u2716])/;

/**
 * The applicant's explicit Yes/No per country (profile "Authorized to work in
 * the US / in Canada", 2026-10-03). It answers exactly the question an
 * employer asks, so it wins over the free-text reading for that country. Not
 * authorized there means sponsorship is needed to work there; authorized keeps
 * what the text said about the future (OPT: authorized now, sponsored later).
 * An answer that agrees with the text keeps the text's richer basis: a
 * "Canadian citizen" who says Yes for Canada is still a citizen of Canada (the
 * citizenship questions read it).
 */
function applyExplicitCountries(profile: UserApplicationProfile, byCountry: Map<string, CountryAuth>): void {
  const explicit: Array<[string, string | undefined]> = [
    ["US", profile.authorizedUS],
    ["CA", profile.authorizedCanada],
  ];
  for (const [code, stated] of explicit) {
    const p = polarityOf(stated ?? "");
    if (p === null) continue;
    const prev = byCountry.get(code);
    if (prev && prev.authorized === p) continue;
    // A Yes keeps what the status was: F-1 OPT is authorized AND a student
    // visa, and rewritten as "stated" it was no longer known not to be
    // citizenship (Covar's "Are you a US citizen?", question bank 2026-10-05).
    byCountry.set(code, {
      authorized: p,
      needsSponsorship: p ? (prev?.needsSponsorship ?? null) : true,
      basis: p && prev ? prev.basis : "stated",
      ...(p && prev?.employerTied ? { employerTied: true } : {}),
    });
  }
}

/**
 * Is the applicant authorized to work in `countryCode` (null = the question
 * names no country and none could be inferred)? High-confidence answers only
 * come from a statement that covers that country.
 */
/** The EU, the rest of the EEA and Switzerland: a citizen of one may work in
 *  any other (freedom of movement). */
const FREE_MOVEMENT = new Set(["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "CH"]);

/** A citizen of a free-movement country asked about another one: "Are you
 *  legally authorized to work in Austria?" was blank for a German citizen
 *  (Genetec on Workable, 2026-10-05). */
function freeMovementCitizen(auth: WorkAuthFacts, countryCode: string): boolean {
  return FREE_MOVEMENT.has(countryCode) && [...auth.byCountry.entries()].some(([cc, a]) => a.basis === "citizen" && a.authorized === true && FREE_MOVEMENT.has(cc));
}

export function authorizedIn(auth: WorkAuthFacts, countryCode: string | null, residence: string | null): Fact<boolean> | null {
  if (countryCode) {
    const c = auth.byCountry.get(countryCode);
    if (c && c.authorized !== null) return fact(c.authorized, "high", `work-auth:${c.basis}`);
    if (!c && freeMovementCitizen(auth, countryCode)) return fact(true, "high", "work-auth:free-movement");
    // A statement that named no country, about a question that names the
    // applicant's own country of residence: the statement was made about the
    // place they live and apply.
    if (auth.byCountry.size === 0 && auth.unscopedAuthorized !== null && residence === countryCode) {
      return fact(auth.unscopedAuthorized, "high", "work-auth:unscoped-residence");
    }
    return null;
  }
  // No country in the question.
  if (auth.byCountry.size === 0 && auth.unscopedAuthorized !== null) {
    return fact(auth.unscopedAuthorized, "high", "work-auth:unscoped");
  }
  return null;
}

/** Does the applicant need sponsorship (now or in future) to work in `countryCode`? */
export function needsSponsorshipIn(auth: WorkAuthFacts, countryCode: string | null, residence: string | null): Fact<boolean> | null {
  const stated = auth.statedSponsorship;
  if (countryCode) {
    const c = auth.byCountry.get(countryCode);
    // Not authorized there: working there takes sponsorship, whatever the
    // general answer says (a Canadian's "No" is about Canada). Live
    // 2026-10-03: "authorized in the US: No" plus "requires sponsorship: No"
    // answered "No, I do not require sponsorship" for a job in the USA.
    if (c && c.authorized === false) return fact(true, "high", `sponsorship:not-authorized-${c.basis}`);
    // A citizen or permanent resident never needs sponsorship there, whatever
    // the general answer says: a German citizen's "Yes" is about North
    // America, and "a visa to remain in your current location" (Berlin) got
    // it (GitLab, question bank 2026-10-05).
    if (c && (c.basis === "citizen" || c.basis === "permanent_resident")) return fact(false, "high", `sponsorship:${c.basis}`);
    if (!c && freeMovementCitizen(auth, countryCode)) return fact(false, "high", "sponsorship:free-movement");
    // Every US work visa and student status is temporary: a "No" beside OPT
    // or H-1B is not believed, and not contradicted either.
    if (stated === false && c && countryCode === "US" && (c.basis === "work_permit" || c.basis === "student")) return null;
    // Nor beside a status a veto withheld there (decide-b round 5).
    if (stated === false && !c && auth.withheld?.includes(countryCode)) return null;
    // The applicant's own answer wins for any country their status covers.
    if (stated !== null && (c || (auth.byCountry.size === 0 && (!residence || residence === countryCode)))) {
      return fact(stated, "high", "sponsorship:stated");
    }
    if (c && c.needsSponsorship !== null) return fact(c.needsSponsorship, "high", `sponsorship:${c.basis}`);
    return null;
  }
  // No country in the question: only the applicant's own answer settles it,
  // and only when their status is not confined to some OTHER country. Not
  // authorized somewhere, the question may well be about that place.
  if (stated !== null && [...auth.byCountry.values()].some((c) => c.authorized === false)) {
    return fact(stated, "medium", "sponsorship:stated-unscoped-not-everywhere");
  }
  if (stated !== null) {
    if (auth.byCountry.size === 0 || (residence && auth.byCountry.has(residence)) || auth.byCountry.size === 1) {
      return fact(stated, "high", "sponsorship:stated-unscoped");
    }
    return fact(stated, "medium", "sponsorship:stated-multi-country");
  }
  if (auth.byCountry.size === 1 && !auth.sponsorshipUnclear) {
    const [only] = [...auth.byCountry.values()];
    if (only.needsSponsorship === false) return fact(false, "medium", `sponsorship:${only.basis}-unscoped`);
  }
  return null;
}

// ---------------------------------------------------------------------------
// Employment
// ---------------------------------------------------------------------------

export interface EmploymentFacts {
  /** Merged, fractional years across all dated rows (overlaps counted once). */
  totalYears: Fact<number> | null;
  /** The same from the dated rows alone, whatever figure the profile states:
   *  a stated "3" may count only a new career. */
  datedYears: Fact<number> | null;
  /** The dated rows' years without internships, co-ops and student jobs: "Do
   *  not include time from co-ops, internships, or school work" (Heron Power,
   *  Ashby bank, round 8). */
  fullTimeYears: Fact<number> | null;
  currentCompany: Fact<string> | null;
  currentTitle: Fact<string> | null;
  /** The most recent row whether or not it is current ("previous employer"). */
  mostRecentCompany: Fact<string> | null;
  currentlyEmployed: Fact<boolean> | null;
  /** Every employer named on the profile, for "have you worked here?". */
  employers: string[];
  /** Every title, for domain checks ("software development experience"). */
  titles: string[];
}

export function employmentFacts(profile: UserApplicationProfile, today: Date): EmploymentFacts {
  const rows = (profile.experience ?? []).filter((r) => r && (r.company?.trim() || r.title?.trim()));
  const now = startOfDay(today);
  const spans: Array<{ start: Date; end: Date; current: boolean; row: (typeof rows)[number] }> = [];
  let undatable = 0;
  for (const r of rows) {
    const s = parseDateSpan(r.startDate || "");
    if (!s) {
      undatable++;
      continue;
    }
    const endText = (r.endDate || "").trim();
    const current = !endText || PRESENT_RE.test(endText);
    let end: Date;
    if (current) end = now;
    else {
      const e = parseDateSpan(endText);
      if (!e) {
        undatable++;
        continue;
      }
      end = e.latest < now ? e.latest : now;
    }
    if (end < s.earliest) {
      undatable++;
      continue;
    }
    spans.push({ start: s.earliest, end, current, row: r });
  }

  // Years: merged intervals. Only high when EVERY row was datable; a row we
  // could not read might be years long, so the total would be a lower bound.
  let totalYears: Fact<number> | null = null;
  let datedYears: Fact<number> | null = null;
  const mergedYears = (list: typeof spans): number => {
    const sorted = [...list].sort((a, b) => a.start.getTime() - b.start.getTime());
    const merged: Array<[Date, Date]> = [];
    for (const { start, end } of sorted) {
      const last = merged[merged.length - 1];
      if (last && start <= last[1]) {
        if (end > last[1]) last[1] = end;
      } else merged.push([start, end]);
    }
    const days = merged.reduce((sum, [a, b]) => sum + (b.getTime() - a.getTime()) / DAY_MS + 1, 0);
    return Math.round((days / 365.25) * 100) / 100;
  };
  if (spans.length > 0) {
    datedYears = fact(mergedYears(spans), undatable === 0 ? "high" : "medium", "experience:merged-spans");
  }
  const STUDENT_JOB = /\b(intern|internship|co ?-?op|placement|student|apprentice|trainee|teaching assistant|research assistant|graduate assistant)\b/i;
  const fullTime = spans.filter((s) => !STUDENT_JOB.test(s.row.title || ""));
  const fullTimeYears: Fact<number> | null = spans.length > 0 ? fact(mergedYears(fullTime), undatable === 0 ? "high" : "medium", "experience:full-time-spans") : null;
  const stated = parseFloat((profile.yearsOfExperience || "").replace(/[^0-9.]/g, ""));
  if (profile.yearsOfExperience?.trim() && Number.isFinite(stated)) {
    totalYears = fact(stated, "high", "profile:yearsOfExperience");
  } else {
    totalYears = datedYears;
  }

  const currentRows = spans.filter((s) => s.current).sort((a, b) => b.start.getTime() - a.start.getTime());
  const latestRow = [...spans].sort((a, b) => b.end.getTime() - a.end.getTime() || b.start.getTime() - a.start.getTime())[0];

  let currentCompany: Fact<string> | null = null;
  let currentTitle: Fact<string> | null = null;
  // A yes/no stored where a title or company belongs (a real profile held the
  // job title "No", 2026-10-03) is no title: it would be typed as one.
  const NOT_A_NAME = /^(yes|no|y|n|true|false|n\/?a|none|null|-+)$/i;
  if (profile.currentCompany?.trim() && !NOT_A_NAME.test(profile.currentCompany.trim())) {
    currentCompany = fact(profile.currentCompany.trim(), "high", "profile:currentCompany");
  }
  if (profile.currentTitle?.trim() && !NOT_A_NAME.test(profile.currentTitle.trim())) {
    currentTitle = fact(profile.currentTitle.trim(), "high", "profile:currentTitle");
  }
  if (currentRows.length === 1) {
    const r = currentRows[0].row;
    if (!currentCompany && r.company?.trim()) currentCompany = fact(r.company.trim(), "high", "experience:current-row");
    if (!currentTitle && r.title?.trim()) currentTitle = fact(r.title.trim(), "high", "experience:current-row");
  } else if (currentRows.length > 1) {
    // Two concurrent current jobs: which one a form means is a judgment call.
    const r = currentRows[0].row;
    if (!currentCompany && r.company?.trim()) currentCompany = fact(r.company.trim(), "medium", "experience:latest-current-row");
    if (!currentTitle && r.title?.trim()) currentTitle = fact(r.title.trim(), "medium", "experience:latest-current-row");
  }

  const mostRecentCompany = latestRow?.row.company?.trim()
    ? fact(latestRow.row.company.trim(), undatable === 0 ? "high" : "medium", "experience:most-recent-row")
    : null;

  let currentlyEmployed: Fact<boolean> | null = null;
  if (currentRows.length > 0) currentlyEmployed = fact(true, "high", "experience:current-row");
  else if (spans.length > 0 && undatable === 0) currentlyEmployed = fact(false, "medium", "experience:all-rows-ended");

  return {
    totalYears,
    datedYears,
    fullTimeYears,
    currentCompany,
    currentTitle,
    mostRecentCompany,
    currentlyEmployed,
    employers: rows.map((r) => (r.company || "").trim()).filter(Boolean),
    titles: rows.map((r) => (r.title || "").trim()).filter(Boolean),
  };
}

// ---------------------------------------------------------------------------
// Education
// ---------------------------------------------------------------------------

/** Tier rank (higher = more senior) and its canonical label. */
// Quebec's degrees in French too ("Baccalauréat en psychologie", a Montreal
// applicant, 2026-10-03): only "baccalauréat en / ès …" is a bachelor's, since
// France's bare baccalauréat is the high-school diploma.
export const DEGREE_TIERS: Array<{ rank: number; label: string; re: RegExp }> = [
  { rank: 6, label: "Doctorate", re: /\b(ph\.?\s?d|doctorate|doctoral|doctor of philosophy|d\.?phil|ed\.?d|doctorat)\b/i },
  { rank: 5, label: "Master's Degree", re: /\b(master'?s?|m\.\s?(sc|s|a|eng|ed)\.?|msc|meng|mba|m\.?b\.?a|mfa|mph|llm)\b|\bma[iî]trise\b/i },
  { rank: 4, label: "Bachelor's Degree", re: /\b(bachelor'?s?|baccalaureate|b\.\s?(sc|s|a|eng|comm|ed|tech|f\.?a)\.?|bsc|basc|beng|bcomm|bba|btech|undergraduate)\b|\bbaccalaur[ée]at (en|[èe]s)\s/i },
  { rank: 3, label: "Associate Degree", re: /\b(associate'?s?( degree)?|a\.\s?(a|s)\.)\b/i },
  { rank: 2, label: "Diploma", re: /\b(diploma|certificate|college diploma|advanced diploma|certificat)\b|\bdipl[ôo]me\b/i },
  { rank: 1, label: "High School", re: /\b(high school|secondary school|ged)\b/i },
];

export function degreeRank(text: string): number | null {
  // A "High School Diploma" is a high school, not a college diploma (Epic
  // Games' Degree, live 2026-10-05).
  if (/\b(high school|secondary school)\b/i.test(text || "") && !DEGREE_TIERS.some((t) => t.rank >= 3 && t.re.test(text))) return 1;
  for (const t of DEGREE_TIERS) if (t.re.test(text || "")) return t.rank;
  return null;
}

/**
 * One education row's graduation as text, refined by the stated expected
 * graduation MONTH ("2027-04") when it is the row graduating that year (or the
 * only row, undated): what split month / year controls fill from.
 */
export function graduationOfRow(profile: UserApplicationProfile, index: number): string {
  const rows = profile.education ?? [];
  const own = (rows[index]?.graduationYear ?? "").trim();
  const m = /^(\d{4})-(\d{2})$/.exec((profile.expectedGraduation ?? "").trim());
  if (!m) return own;
  if (!own) return rows.length === 1 ? m[0] : own;
  return own === m[1] ? m[0] : own;
}

export interface EducationEntryFacts {
  school: string;
  degree: string;
  rank: number | null;
  graduation: DateSpan | null;
  /** true: finished; false: still enrolled; null: cannot tell. */
  completed: boolean | null;
}

export interface EducationFacts {
  entries: EducationEntryFacts[];
  /** Highest level, completed or not. */
  highestRank: Fact<number> | null;
  /** Highest level the applicant has FINISHED. */
  highestCompletedRank: Fact<number> | null;
  /** The entry a "your school / your degree" question means: in-progress first,
   *  else the most recent graduation. */
  primary: EducationEntryFacts | null;
  currentlyEnrolled: Fact<boolean> | null;
}

export function educationFacts(profile: UserApplicationProfile, today: Date): EducationFacts {
  const now = startOfDay(today);
  const rows = (profile.education ?? []).filter((e) => e && (e.school?.trim() || e.degree?.trim()));
  // The stated expected graduation MONTH ("2027-04") refines the row it belongs
  // to: the one graduating that year (or the only row, when it has no date).
  const expected = parseDateSpan((profile.expectedGraduation ?? "").trim());
  const refine = (g: DateSpan | null): DateSpan | null => {
    if (!expected || expected.precision === "year") return g;
    if (!g) return rows.length === 1 ? expected : g;
    return g.precision === "year" && g.earliest.getUTCFullYear() === expected.earliest.getUTCFullYear() ? expected : g;
  };
  const entries: EducationEntryFacts[] = rows
    .map((e) => {
      const gradText = String(e.graduationYear ?? "").trim();
      const graduation = refine(parseDateSpan(gradText));
      let completed: boolean | null = null;
      // A résumé's education end date of "Present" (the backend maps the end
      // date to graduationYear) means still studying: the date is unknown, the
      // status is not (a real profile, 2026-10-03).
      if (!graduation && /^(present|current|currently|ongoing|now|in progress|to date)$/i.test(gradText)) completed = false;
      if (graduation) {
        if (graduation.latest < now) completed = true;
        else if (graduation.earliest > now) completed = false;
        else completed = null; // graduating this very year/month: cannot tell
        // A year-only graduation in the current year: most programs finish by
        // summer, so after June it is done; before, it is still running.
        if (completed === null && graduation.precision === "year") {
          completed = now.getUTCMonth() >= 6 ? true : null;
        }
      }
      return { school: (e.school || "").trim(), degree: (e.degree || "").trim(), rank: degreeRank(e.degree || ""), graduation, completed };
    });

  const ranks = entries.map((e) => e.rank).filter((r): r is number => r !== null);
  const highestRank = ranks.length ? fact(Math.max(...ranks), entries.every((e) => e.rank !== null) ? "high" : "medium", "education:tiers") : null;

  // Highest COMPLETED: every entry above it must be known-incomplete. When
  // every entry is known to be still in progress, the applicant holds none of
  // the listed degrees yet (rank 0): "Do you have a Bachelor's?" is a No.
  let highestCompletedRank: Fact<number> | null = null;
  const done = entries.filter((e) => e.completed === true && e.rank !== null).map((e) => e.rank as number);
  const unknownAbove = entries.some((e) => e.completed === null && e.rank !== null && e.rank > Math.max(0, ...done));
  if (done.length && !unknownAbove) highestCompletedRank = fact(Math.max(...done), "high", "education:completed-tiers");
  else if (entries.length > 0 && entries.every((e) => e.completed === false)) {
    highestCompletedRank = fact(0, "high", "education:none-completed-yet");
  }

  const enrolled = entries.filter((e) => e.completed === false);
  const primary =
    enrolled[0] ??
    [...entries].sort((a, b) => (b.graduation?.latest.getTime() ?? 0) - (a.graduation?.latest.getTime() ?? 0))[0] ??
    null;

  let currentlyEnrolled: Fact<boolean> | null = null;
  if (enrolled.length) currentlyEnrolled = fact(true, "high", "education:graduation-in-future");
  // Every listed program finished: not a student. The profile is the
  // applicant's own account of their schooling; read as only "medium", a
  // graduate's "currently enrolled in a CS program?" went to the AI (Zoox on
  // Lever, live 2026-10-03).
  else if (entries.length && entries.every((e) => e.completed === true)) currentlyEnrolled = fact(false, "high", "education:all-graduated");

  return { entries, highestRank, highestCompletedRank, primary, currentlyEnrolled };
}

// ---------------------------------------------------------------------------
// Availability
// ---------------------------------------------------------------------------

export interface AvailabilityFacts {
  earliestStart: Fact<Date> | null;
  noticeDays: Fact<number> | null;
  /** The stated notice period text, verbatim, for free-text questions. */
  noticeText: string | null;
}

/** "2 weeks" → 14, "1 month" → 30, "immediately" → 0; null when unreadable. */
export function noticeToDays(text: string): number | null {
  const t = (text || "").trim().toLowerCase();
  if (!t) return null;
  if (/\b(immediate(ly)?|none|no notice|asap|right away|0)\b/.test(t) && !/\d+\s*(day|week|month)/.test(t)) return 0;
  const m = t.match(/(\d+(?:\.\d+)?)\s*(day|week|wk|month|mo)/);
  if (!m) return null;
  const n = parseFloat(m[1]);
  const unit = m[2];
  if (unit.startsWith("d")) return Math.round(n);
  if (unit.startsWith("w")) return Math.round(n * 7);
  return Math.round(n * 30);
}

export function availabilityFacts(profile: UserApplicationProfile, today: Date): AvailabilityFacts {
  const now = startOfDay(today);
  const noticeText = profile.noticePeriod?.trim() || null;
  const days = noticeText ? noticeToDays(noticeText) : null;
  const startSpan = parseDateSpan(profile.earliestStartDate || "");
  let earliestStart: Fact<Date> | null = null;
  if (startSpan && startSpan.precision === "day") {
    // A stated start date in the past means "as soon as possible" now.
    earliestStart = fact(startSpan.earliest < now ? now : startSpan.earliest, "high", "profile:earliestStartDate");
  } else if (startSpan) {
    earliestStart = fact(startSpan.earliest < now ? now : startSpan.earliest, "medium", "profile:earliestStartDate-partial");
  } else if (days !== null) {
    earliestStart = fact(new Date(now.getTime() + days * DAY_MS), "high", "notice-period:today-plus-notice");
  }
  let noticeDays: Fact<number> | null = null;
  if (days !== null) noticeDays = fact(days, "high", "profile:noticePeriod");
  else if (startSpan && startSpan.precision === "day" && startSpan.earliest >= now) {
    noticeDays = fact(Math.round((startSpan.earliest.getTime() - now.getTime()) / DAY_MS), "medium", "start-date:minus-today");
  }
  return { earliestStart, noticeDays, noticeText };
}

// ---------------------------------------------------------------------------
// Age
// ---------------------------------------------------------------------------

/** (youngest, oldest) the applicant can be today, or null. */
export function ageBounds(profile: UserApplicationProfile, today: Date): [number, number] | null {
  const span = parseDateSpan(profile.dateOfBirth || "");
  if (!span) return null;
  const ageOn = (born: Date): number => {
    let a = today.getUTCFullYear() - born.getUTCFullYear();
    if (today.getUTCMonth() < born.getUTCMonth() || (today.getUTCMonth() === born.getUTCMonth() && today.getUTCDate() < born.getUTCDate())) a--;
    return a;
  };
  const youngest = ageOn(span.latest);
  const oldest = ageOn(span.earliest);
  if (oldest < 0 || oldest > 110) return null;
  return [youngest, oldest];
}

// ---------------------------------------------------------------------------
// Everything at once
// ---------------------------------------------------------------------------

export interface ProfileFacts {
  location: LocationFacts;
  workAuth: WorkAuthFacts;
  employment: EmploymentFacts;
  education: EducationFacts;
  availability: AvailabilityFacts;
  age: [number, number] | null;
  today: Date;
}

const cache = new WeakMap<UserApplicationProfile, { day: string; facts: ProfileFacts }>();

/** All facts for a profile, memoized per profile object and calendar day. */
export function profileFacts(profile: UserApplicationProfile, today: Date = new Date()): ProfileFacts {
  const day = today.toISOString().slice(0, 10);
  const hit = cache.get(profile);
  if (hit && hit.day === day) return hit.facts;
  const facts: ProfileFacts = {
    location: locationFacts(profile),
    workAuth: workAuthFacts(profile),
    employment: employmentFacts(profile, today),
    education: educationFacts(profile, today),
    availability: availabilityFacts(profile, today),
    age: ageBounds(profile, today),
    today,
  };
  cache.set(profile, { day, facts });
  return facts;
}
