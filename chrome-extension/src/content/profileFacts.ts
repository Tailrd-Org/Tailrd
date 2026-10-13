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
  countryByCode,
  countryFromName,
  countryHintForCity,
  findPostalCode,
  regionFromText,
  type Country,
  type Region,
} from "./geo";

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
}

export interface WorkAuthFacts {
  /** Per ISO country code. Only countries the profile actually speaks about. */
  byCountry: Map<string, CountryAuth>;
  /** The applicant's own yes/no on "do you require sponsorship", if stated. */
  statedSponsorship: boolean | null;
  /** A statement that named NO country ("Yes", "Authorized to work"). */
  unscopedAuthorized: boolean | null;
}

/** "N/A" and its spellings, read against a trimmed, lowercased answer. */
const NOT_APPLICABLE = /^(n\s*[/.]?\s*a\b\.?|not applicable\b|does not apply\b|doesn'?t apply\b)/;

/** Yes/no reading of a short stored answer, or null. */
export function polarityOf(text: string): boolean | null {
  const t = (text || "").trim().toLowerCase();
  if (!t) return null;
  // "N/A", "NA", "N.A.", "not applicable": unanswered, never a No (owner
  // decisions 2026-10-13; "n" before "/" read as "No").
  if (NOT_APPLICABLE.test(t)) return null;
  if (/^(yes|y|true|oui|si)\b/.test(t)) return true;
  if (/^(no|n|false|non|none|not)\b/.test(t)) return false;
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
  const fullText = (profile.workAuthorization || "").trim();
  const byCountry = new Map<string, CountryAuth>();
  const statedSponsorship = polarityOf(profile.requiresSponsorship || "");
  let unscopedAuthorized: boolean | null = null;

  // "Canada but not in the US": the negated clause names countries that are
  // never authorized, and the rest of the sentence is read without it, so the
  // status it states belongs only to the countries named positively.
  const scope = negatedCountryScope(fullText);
  const text = scope.positiveText;
  const lower = text.toLowerCase();
  const countries = countriesIn(text).filter((c) => !scope.negated.has(c.code));
  // A statement that limits itself ("only in Canada", "but not the US") is an
  // authorization statement for the countries it keeps.
  const scoped = scope.negated.size > 0 || /\bonly\b/.test(lower);
  const set = (code: string, a: CountryAuth): void => {
    byCountry.set(code, a);
  };

  const denied = /\b(not|no|never)\s+(legally\s+)?(authori[sz]ed|eligible|entitled|permitted|allowed)\b|\bno work (authori[sz]ation|permit)\b|\bnot a (citizen|resident)\b/.test(lower);
  const citizen = /\bcitizen(ship)?\b|\bnational\b/.test(lower);
  const pr = /\bpermanent resident\b|\bgreen card\b|\blanded immigrant\b|\blawful permanent\b|\bpr\b/.test(lower);
  const student = /\b(student visa|study permit|f-?1|j-?1|m-?1|international student)\b/.test(lower);
  const sponsoredVisa = /\b(h-?1b|l-?1|e-?3|o-?1|tn)\b/.test(lower);
  const permit = /\b(work permit|open work permit|pgwp|post-graduation work permit|ead|employment authori[sz]ation|work visa|opt|cpt)\b/.test(lower);
  const affirmative = /\b(authori[sz]ed|eligible|entitled|permitted|allowed|legally able|right) to work\b|\bcan (legally )?work\b/.test(lower);
  const bareYes = polarityOf(text);

  if (countries.length === 0 && scope.negated.size === 0) {
    if (denied || bareYes === false) unscopedAuthorized = false;
    else if (affirmative || bareYes === true || citizen || pr) unscopedAuthorized = true;
    applyExplicitCountries(profile, byCountry);
    return { byCountry, statedSponsorship, unscopedAuthorized };
  }

  for (const c of countries) {
    if (denied) set(c.code, { authorized: false, needsSponsorship: null, basis: "denied" });
    else if (citizen && !pr && !student && !permit) set(c.code, { authorized: true, needsSponsorship: false, basis: "citizen" });
    else if (pr && !student && !permit) set(c.code, { authorized: true, needsSponsorship: false, basis: "permanent_resident" });
    else if (sponsoredVisa) set(c.code, { authorized: true, needsSponsorship: true, basis: "work_permit" });
    else if (student) set(c.code, { authorized: null, needsSponsorship: null, basis: "student" });
    else if (permit) set(c.code, { authorized: true, needsSponsorship: null, basis: "work_permit" });
    else if (affirmative || bareYes === true || scoped) set(c.code, { authorized: true, needsSponsorship: null, basis: "statement" });
  }
  // A country the sentence excludes outright ("not in the US", "except the
  // US") is a No. One it only says the applicant is not a citizen of ("not a
  // US citizen") stays unknown: a green card holder says that too.
  for (const [code, outright] of scope.negated) {
    if (outright) set(code, { authorized: false, needsSponsorship: null, basis: "denied" });
  }
  applyExplicitCountries(profile, byCountry);
  return { byCountry, statedSponsorship, unscopedAuthorized };
}

/** A negation's reach ends at the next clause. "and" ends it only before a new
 *  statement ("not in the US and authorized in Canada"); between two countries
 *  ("not in the US and the UK") it keeps both negated. */
const CLAUSE_BREAK =
  /[,;.!?()]|\b(but|however|whereas|while|although|though|yet)\b|\band\s+(?=(i|am|is|authori[sz]ed|eligible|entitled|permitted|allowed|legally|can|have|hold|a|an)\b)/;
/** Words that put the countries after them outside the statement. */
const NEGATION = /\b(not|never|except|excluding|other than|outside( of)?)\b/g;
/** A negated clause about sponsorship ("not requiring sponsorship in the US")
 *  says nothing against the country's authorization. */
const SPONSORSHIP_WORDS = /\b(sponsor\w*|requir\w*|need\w*)\b/;

/**
 * Split a work-authorization statement into the countries it negates and the
 * text that is left. Each negation word reaches to the next clause break:
 *
 *   "Authorized to work in Canada but not in the US"  negated US (outright)
 *   "Not authorized to work in the US, but in Canada"  negated US (outright)
 *   "Canadian citizen, not a US citizen"               negated US (not outright)
 *
 * "Outright" means the clause excludes the country itself, not a status in it
 * ("not a ... citizen / resident"), so the country is a No and not unknown.
 */
export function negatedCountryScope(text: string): { negated: Map<string, boolean>; positiveText: string } {
  const negated = new Map<string, boolean>();
  const lower = text.toLowerCase();
  const cuts: Array<[number, number]> = [];
  const matches = [...lower.matchAll(NEGATION)];
  matches.forEach((m, i) => {
    const from = (m.index ?? 0) + m[0].length;
    // A clause also ends at the next negation, which starts its own: the
    // clauses never overlap, so a long run of negations stays linear.
    const limit = i + 1 < matches.length ? (matches[i + 1].index ?? lower.length) : lower.length;
    const window = lower.slice(from, limit);
    const brk = window.search(CLAUSE_BREAK);
    const to = from + (brk >= 0 ? brk : window.length);
    const clause = lower.slice(from, to);
    if (SPONSORSHIP_WORDS.test(clause)) return;
    // Same indices in the original text: countriesIn reads "US" by its capitals.
    const named = countriesIn(text.slice(from, to));
    if (named.length === 0) return;
    const outright = !/^\s*(an?|the applicant an?)\s/.test(clause) && !/\b(citizen|national|resident)\b/.test(clause);
    for (const c of named) negated.set(c.code, (negated.get(c.code) ?? false) || outright);
    cuts.push([m.index ?? 0, to]);
  });
  const kept: string[] = [];
  let at = 0;
  for (const [a, b] of cuts) {
    kept.push(text.slice(at, a));
    at = b;
  }
  kept.push(text.slice(at));
  return { negated, positiveText: kept.join(" ").trim() };
}

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
  if (auth.byCountry.size === 1) {
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
  if (spans.length > 0) {
    const sorted = [...spans].sort((a, b) => a.start.getTime() - b.start.getTime());
    const merged: Array<[Date, Date]> = [];
    for (const { start, end } of sorted) {
      const last = merged[merged.length - 1];
      if (last && start <= last[1]) {
        if (end > last[1]) last[1] = end;
      } else merged.push([start, end]);
    }
    const days = merged.reduce((sum, [a, b]) => sum + (b.getTime() - a.getTime()) / DAY_MS + 1, 0);
    const years = Math.round((days / 365.25) * 100) / 100;
    datedYears = fact(years, undatable === 0 ? "high" : "medium", "experience:merged-spans");
  }
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
