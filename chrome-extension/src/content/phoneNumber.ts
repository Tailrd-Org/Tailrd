/**
 * Phone numbers beside a dialing-code picker. A form that takes the code in
 * its own dropdown wants only the national part in the number box: typed
 * whole, "+49 30 12345678" became "493012345678" under the "+44" Rippling had
 * preset from the page's locale (live 2026-10-05), a number that reaches no
 * one. The picker itself is set from the number's own code, which can differ
 * from the country the applicant lives in.
 */
import { COUNTRIES, DIAL_CODES, countryFromName, type Country } from "./geo";

const KNOWN_CODES = new Set(Object.values(DIAL_CODES));

/** The dialing code a number is written with ("49" for "+49 30 …"), or null. */
export function dialCodeOf(phone: string): string | null {
  const m = /^\s*\+\s*(\d+)/.exec(phone);
  if (!m) return null;
  // Dialing codes are prefix-free, so the first known prefix is the code.
  for (let n = 1; n <= Math.min(4, m[1].length); n++) {
    const code = m[1].slice(0, n);
    if (KNOWN_CODES.has(code)) return code;
  }
  return null;
}

/** The number without its dialing code (and a written "(0)" trunk prefix),
 *  the rest kept as written. A number with no code is returned unchanged. */
export function nationalNumber(phone: string): string {
  const code = dialCodeOf(phone);
  if (!code) return phone;
  const rest = phone.replace(new RegExp(`^\\s*\\+\\s*${code}[\\s.\\-]*(\\(0\\)[\\s.\\-]*)?`), "").trim();
  return rest || phone;
}

/**
 * The country a number is written for: the one its code names, the home
 * country breaking a shared code (+1 is the US and Canada alike). A shared
 * code abroad says nothing, null. A number with no code is the home country's.
 */
export function phoneCountryName(phone: string, home: Country | null): string | null {
  const code = dialCodeOf(phone);
  if (!code) return /^\s*\+/.test(phone) ? null : home?.name ?? null;
  if (home && DIAL_CODES[home.code] === code) return home.name;
  const owners = COUNTRIES.filter((c) => DIAL_CODES[c.code] === code);
  return owners.length === 1 ? owners[0].name : null;
}

/**
 * The number for a box that shows its dialing code ("+1", Recruitee): such a
 * box is in international form, and "(416) 555-0142" typed without its code
 * was read as "+41 65 550 14 2", Switzerland (live 2026-10-05). A number
 * written with its own code goes in whole; otherwise the home country's code
 * comes first, else the one the box shows.
 */
export function internationalNumber(phone: string, homeCountry: string | null, shownCode: string | null): string {
  const p = phone.trim();
  if (/^\+/.test(p)) return p;
  const home = homeCountry ? countryFromName(homeCountry) : null;
  const code = (home && DIAL_CODES[home.code]) || shownCode;
  return code ? `+${code} ${p}` : p;
}

/** A dialing code shown as a picker's value or option: "+44 GB", "+1",
 *  "+49 DE - Germany", "Germany (+49)", "India +91", "(+93) Afghanistan"
 *  (iCIMS, live 2026-10-17). */
const DIAL_TEXT = /^\+\s?\d{1,4}\b|^\(\s*\+\s?\d{1,4}\s*\)|\(\s*\+\s?\d{1,4}\s*\)\s*$|\s\+\d{1,4}\s*$/;

/** A picker that holds dialing codes: its value is one, or nearly every
 *  option is (a native list carries its options at scan time). */
export function looksLikeDialCodes(value: string, options: string[] | undefined): boolean {
  if (DIAL_TEXT.test(value.trim())) return true;
  const real = (options ?? []).map((o) => o.trim()).filter((o) => o && !/^(-+|select\b.*|choose\b.*)$/i.test(o));
  return real.length >= 5 && real.filter((o) => DIAL_TEXT.test(o)).length >= real.length * 0.8;
}
