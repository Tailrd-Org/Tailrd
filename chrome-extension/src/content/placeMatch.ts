/**
 * Choosing a PLACE from a list of place suggestions ("Toronto, ON, CAN",
 * "Toronto, Ontario, Canada", "Toronto, OH, US", "Toronto, New South Wales,
 * Australia"), compared as places: city, then province/state (by code or
 * name), then country (by name, alias, ISO-2 or ISO-3).
 *
 * Word or token matching is wrong here in both directions: "Canada" never
 * matches the ISO-3 "CAN", and token overlap drops short tokens, which made
 * "Toronto, OH, US" look like a PERFECT match for "Toronto, ON, Canada" (its
 * only long token is "toronto"). Exactly one suggestion must agree with every
 * part the wanted place states, or none is chosen.
 */
import { countryByCode, countryFromName, regionFromText } from "./geo";

const norm = (s: string): string =>
  (s || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();

export interface Place {
  city: string;
  region: string | null;
  country: string | null;
  /** A qualifier after the city that is neither a known region nor a known
   *  country ("Costa Rica", "Batangas"): the place is SOMEWHERE, not silent. */
  other?: boolean;
}

/** City, province/state and country of "Toronto, ON, CAN" / "Toronto, Ontario, Canada". */
export function placeOf(text: string): Place {
  const parts = (text || "").split(",").map((p) => p.trim()).filter(Boolean);
  // The country is the LAST part naming one in full ("…, Georgia, United
  // States" is the state). A two-letter part is then read as a region first:
  // "San Jose, CA, United States" is California, not Canada (it picked "San
  // José, Costa Rica" on Zipline's embedded Greenhouse form, live 2026-10-03).
  let country: string | null = null;
  let countryAt = -1;
  for (let i = parts.length - 1; i >= 1; i--) {
    const c = parts[i].length > 2 ? countryFromName(parts[i]) : null;
    if (c) {
      country = c.code;
      countryAt = i;
      break;
    }
  }
  let region: string | null = null;
  let other = false;
  for (let i = 1; i < parts.length; i++) {
    if (i === countryAt) continue;
    const p = parts[i];
    const r = regionFromText(p, country === "US" || country === "CA" ? country : undefined);
    if (r && !region) {
      region = `${r.country}:${r.code}`;
      if (!country) country = r.country;
      continue;
    }
    if (!country && p.length === 2 && /^(us|ca|uk)$/i.test(p)) {
      const two = countryByCode(p.toUpperCase() === "UK" ? "GB" : p);
      if (two) {
        country = two.code;
        continue;
      }
    }
    other = true;
  }
  return { city: norm(parts[0] ?? ""), region, country, ...(other ? { other } : {}) };
}

/** True when a list reads as place suggestions ("City, Region[, Country]"):
 *  a known state, province or country after the city. A comma alone is no
 *  place: "Yes, I live here" / "Yes, I plan to relocate" were chosen from as
 *  places for a "location" question, and neither was picked (Brex, 2026-10-03). */
export function looksLikePlaces(options: string[]): boolean {
  const real = options.filter((o) => o.trim());
  const places = real.filter((o) => {
    if (!/^[^,]{2,60},\s*\S/.test(o.trim())) return false;
    const p = placeOf(o);
    // A qualifier we cannot read is still a NAME ("Costa Rica", "Batangas"),
    // where an answer is a phrase ("I live here").
    return Boolean(p.region || p.country) || o.split(",").slice(1).every((part) => isPlaceName(part));
  });
  return places.length >= Math.max(1, Math.ceil(real.length / 2));
}

/** Words that each start with a capital, bar a few joining ones: "Costa Rica",
 *  "New South Wales", "Isle of Man". */
function isPlaceName(text: string): boolean {
  const words = text.trim().split(/\s+/).filter(Boolean);
  return words.length > 0 && words.length <= 5 && words.every((w) => /^\p{Lu}[\p{L}.'’-]*$/u.test(w) || /^(of|and|de|del|la|le|da|do|du|des|the)$/.test(w));
}

/** Index of the one suggestion that is `wanted`, or -1. */
export function pickPlaceOption(suggestions: string[], wanted: string): number {
  const want = placeOf(wanted);
  if (!want.city) return -1;
  // The same suggestion listed twice is one choice, the first: Greenhouse's
  // location lookup returns "Seattle, Washington, United States" twice, and
  // the tie left every Seattle applicant's city blank (live 2026-10-05).
  const hits = suggestions
    .map((s, i) => ({ i, place: placeOf(s) }))
    .filter(({ i }) => suggestions.findIndex((s) => norm(s) === norm(suggestions[i])) === i)
    .filter(
      ({ place }) =>
        place.city === want.city &&
        (!want.region || !place.region || place.region === want.region) &&
        (!want.country || !place.country || place.country === want.country) &&
        // Naming a place we cannot read ("San José, Costa Rica") is not
        // silence: it is no match for a place whose country we know.
        !(place.other && !place.region && !place.country && (want.region || want.country))
    );
  if (hits.length === 1) return hits[0].i;
  // Still several: keep the ones that STATE the region/country the wanted
  // place states (a suggestion silent on the country is weaker than one that
  // agrees).
  const explicit = hits.filter(
    ({ place }) => (!want.region || place.region === want.region) && (!want.country || place.country === want.country)
  );
  if (explicit.length === 1) return explicit[0].i;
  // One city under two districts is one place as far as the profile can say:
  // Lever lists "Bengaluru, Bengaluru Urban, Karnataka, IND" and "Bengaluru,
  // Bangalore, Karnataka, IND" (and two villages of the name elsewhere), and
  // the tie left the box empty (Brillio, live, round 7). Every word of the
  // wanted place between its city and its country ("Karnataka", a region with
  // no code here) must be a part of the suggestion; of those left, the
  // search's own first.
  const parts = (s: string): string[] => s.split(",").map((p) => norm(p)).filter(Boolean);
  const middle = parts(wanted).slice(1).filter((p) => !(p.length > 2 && countryFromName(p)));
  if (explicit.length < 2 || middle.length === 0) return -1;
  const agree = explicit.filter(({ i }) => middle.every((w) => parts(suggestions[i]).includes(w)));
  return agree.length > 0 ? agree[0].i : -1;
}
