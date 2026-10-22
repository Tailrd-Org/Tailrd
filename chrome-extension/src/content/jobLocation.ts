/**
 * Which country the JOB is in, read from structured page data only:
 *
 *   1. schema.org JobPosting JSON-LD (`jobLocation.address.addressCountry`),
 *      which Greenhouse, Lever, Workday, SmartRecruiters and many career
 *      sites embed for search engines;
 *   2. the ATS's own location element (a short "Toronto, ON" / "Remote - US"
 *      line in the posting header).
 *
 * The answer is a country only when every signal found agrees on one; mixed
 * or unparseable locations give null. Questions like "Are you authorized to
 * work in this country?" or an unscoped "Will you require sponsorship?" are
 * answered for THIS country, so a wrong guess here would be a wrong legal
 * answer there: null (and an abstention) is the safe failure.
 *
 * The job's CITY is read the same way (detectJobPlace), for "are you located
 * here, or would you relocate?": only a single, anchored place ("Toronto, ON",
 * "Salt Lake City, Utah, United States") names one; a list of offices does not.
 */
import { countryByCode, countryFromName, regionFromText } from "./geo";
import { parseAddress } from "./profileFacts";

const LOCATION_SELECTORS = [
  ".job__location",
  ".location",
  ".posting-categories .location",
  ".sort-by-location",
  '[data-automation-id="locations"]',
  '[data-ui="job-location"]',
  '[itemprop="jobLocation"]',
  '[class*="job-location" i]',
  '[class*="jobLocation" i]',
  '[class*="posting-location" i]',
  '[data-testid*="location" i]',
  // SmartRecruiters' one-click page names the place only in its top bar
  // (Boston Engineering, live, round 8: no job country on any of its pages).
  '[data-test="topbar-location"]',
  // JazzHR's apply page: a job attribute titled Location (none of the JazzHR
  // bank's 86 pages had a job country, round 8).
  '.job-attributes-container [title="Location"]',
];

const ATS_NAMES = /^(greenhouse|lever|workday|ashby|ashbyhq|workable|smartrecruiters|bamboohr|jobvite|icims|taleo|oracle|successfactors|careers|jobs|job board|job boards)$/i;

export interface JobPlace {
  /** ISO code, or null when not stated. */
  country: string | null;
  /** City name as the page wrote it ("Salt Lake City"), or null. */
  city: string | null;
  /** Every place the posting lists, one per city ("San Francisco, CA"), with
   *  its state or country: "one of our offices" is one of these. */
  places?: string[];
}

/** Several places in one line ("New York, NY; San Francisco, CA", "Toronto or
 *  Remote", "3 Locations") name no single city. */
const MANY_PLACES = /[;|/&•·]|\s(or|and)\s|\blocations\b|\bmultiple\b/i;
const NOT_A_CITY = /\b(remote|hybrid|on ?site|in ?office|office|anywhere|worldwide|global|flexible|various|hq|headquarters|campus)\b/i;

function placeOfText(text: string): JobPlace | null {
  const t = (text || "").replace(/\s+/g, " ").trim();
  if (!t || t.length > 120) return null;
  const whole = countryFromName(t);
  if (whole) return { country: whole.code, city: null };
  // "Canada - Toronto" (Veeva on Lever) is two parts, like "Toronto, Canada".
  const parts = t.replace(/^remote\s*[-–:(]?\s*/i, "").replace(/\)$/, "").replace(/\s+[-–]\s+/g, ", ");
  const parsed = parseAddress(parts);
  const country = parsed.country?.code ?? parsed.region?.country ?? parsed.postal?.country ?? null;
  if (!country) return null;
  // A city counts only when the place around it is named too, and the line
  // names one place: a bare word ("Calgary") or a list is no evidence.
  const city = parsed.city?.trim() ?? "";
  const cityOk = Boolean(city && (parsed.region || parsed.country) && !MANY_PLACES.test(t) && !NOT_A_CITY.test(city));
  return { country, city: cityOk ? city : null };
}

/** One listed place as "City, Region, Country" ("Toronto, ON, Canada" from
 *  "Canada - Toronto"), or null when it names no city with its surroundings. */
function canonicalPlace(text: string): string | null {
  const t = (text || "").replace(/\s+/g, " ").trim();
  if (!t || t.length > 120 || MANY_PLACES.test(t)) return null;
  const parsed = parseAddress(t.replace(/^remote\s*[-–:(]?\s*/i, "").replace(/\)$/, "").replace(/\s+[-–]\s+/g, ", "));
  const city = parsed.city?.trim() ?? "";
  const code = parsed.country?.code ?? parsed.region?.country ?? null;
  if (!city || NOT_A_CITY.test(city) || !code) return null;
  return [city, parsed.region?.code ?? "", countryByCode(code)?.name ?? code].filter(Boolean).join(", ");
}

function countryOfText(text: string): string | null {
  return placeOfText(text)?.country ?? null;
}

/** Text of a schema.org place: a string, or an object's name. */
function placeText(node: unknown): string {
  if (typeof node === "string") return node;
  const n = node as Record<string, unknown> | null | undefined;
  if (!n || typeof n !== "object") return "";
  return typeof n.name === "string" ? n.name : "";
}

function jsonLdPlaces(doc: Document): JobPlace[] {
  const out: JobPlace[] = [];
  for (const s of Array.from(doc.querySelectorAll('script[type="application/ld+json"]'))) {
    let data: unknown;
    try {
      data = JSON.parse(s.textContent || "");
    } catch {
      continue;
    }
    const nodes: unknown[] = Array.isArray(data) ? data : [data];
    for (const node of nodes) {
      const n = node as Record<string, unknown>;
      const graph = Array.isArray(n?.["@graph"]) ? (n["@graph"] as unknown[]) : [n];
      for (const g of graph) {
        const posting = g as Record<string, unknown>;
        if (!posting || !/JobPosting/i.test(String(posting["@type"] ?? ""))) continue;
        const before = out.length;
        const locs = Array.isArray(posting.jobLocation) ? posting.jobLocation : [posting.jobLocation];
        for (const loc of locs) {
          const address = (loc as Record<string, unknown> | undefined)?.address as Record<string, unknown> | string | undefined;
          if (!address) continue;
          if (typeof address === "string") {
            const p = placeOfText(address);
            if (p) out.push(p);
            continue;
          }
          const raw = address.addressCountry;
          const name = typeof raw === "string" ? raw : String((raw as Record<string, unknown> | undefined)?.name ?? "");
          const code = name.length === 2 ? countryByCode(name)?.code : countryFromName(name)?.code;
          const country = code ?? countryOfText([address.addressLocality, address.addressRegion].filter(Boolean).join(", "));
          if (!country) continue;
          const locality = typeof address.addressLocality === "string" ? address.addressLocality.trim() : "";
          const city = locality && !MANY_PLACES.test(locality) && !NOT_A_CITY.test(locality) ? locality : null;
          out.push({ country, city });
        }
        // A remote (TELECOMMUTE) posting states WHERE in applicantLocationRequirements
        // instead (Brex on Greenhouse, live 2026-10-03:
        // {"@type":"Country","name":"Salt Lake City, Utah, United States"}).
        if (out.length === before) {
          const reqs = posting.applicantLocationRequirements;
          for (const r of Array.isArray(reqs) ? reqs : [reqs]) {
            const p = placeOfText(placeText(r));
            if (p) out.push(p);
          }
        }
      }
    }
  }
  return out;
}

/**
 * Every city a posting lists: its JSON-LD offices, else its location line split
 * into places ("San Francisco, CA | New York City, NY | Washington, DC",
 * Anthropic on Greenhouse, live 2026-10-03). Only places naming a city with
 * its state or country.
 */
function listedPlaces(doc: Document): string[] {
  const texts: string[] = [];
  const direct: string[] = [];
  for (const s of Array.from(doc.querySelectorAll('script[type="application/ld+json"]'))) {
    let data: unknown;
    try {
      data = JSON.parse(s.textContent || "");
    } catch {
      continue;
    }
    for (const node of Array.isArray(data) ? data : [data]) {
      const n = node as Record<string, unknown>;
      for (const g of Array.isArray(n?.["@graph"]) ? (n["@graph"] as unknown[]) : [n]) {
        const posting = g as Record<string, unknown>;
        if (!posting || !/JobPosting/i.test(String(posting["@type"] ?? ""))) continue;
        for (const loc of Array.isArray(posting.jobLocation) ? posting.jobLocation : [posting.jobLocation]) {
          const a = (loc as Record<string, unknown> | undefined)?.address as Record<string, unknown> | string | undefined;
          if (typeof a === "string") texts.push(a);
          else if (a) {
            // Built from the fields, never re-parsed: "Toronto, ON, CA" reads
            // CA as California.
            const rawCountry = typeof a.addressCountry === "string" ? a.addressCountry : placeText(a.addressCountry);
            const country = rawCountry.length === 2 ? countryByCode(rawCountry) : countryFromName(rawCountry);
            const city = typeof a.addressLocality === "string" ? a.addressLocality.trim() : "";
            const rawRegion = typeof a.addressRegion === "string" ? a.addressRegion.trim() : "";
            const region = rawRegion ? regionFromText(rawRegion, country?.code === "US" || country?.code === "CA" ? country.code : undefined) : null;
            if (city && country && !MANY_PLACES.test(city) && !NOT_A_CITY.test(city)) {
              direct.push([city, region?.code ?? "", country.name].filter(Boolean).join(", "));
            }
          }
        }
      }
    }
  }
  const out: string[] = [...new Set(direct)];
  const add = (text: string): void => {
    for (const part of text.split(/\s*[|;•·]\s*|\s+or\s+/i)) {
      const place = canonicalPlace(part);
      if (place && !out.includes(place)) out.push(place);
    }
  };
  texts.forEach(add);
  // The first selector that NAMES a place: an element matching an earlier one
  // with none in it (a bare "Location" heading) must not hide a later one
  // that does (Workable's data-ui="job-location").
  if (out.length === 0) {
    for (const sel of LOCATION_SELECTORS) {
      for (const el of Array.from(doc.querySelectorAll(sel)).slice(0, 4)) {
        if (!el.closest("form")) add(el.textContent || "");
      }
      if (out.length) break;
    }
  }
  // A "Location" label and its value: Ashby's "<h2>Location</h2><p>Las Vegas,
  // Nevada</p>" gave the job's city but listed no place, so "our Las Vegas
  // office" had no state to compare (TensorWave, live 2026-10-08).
  if (out.length === 0) labelledValues(doc).forEach(add);
  return out;
}

/** The posting's location line(s) outside any form. */
function elementPlaces(doc: Document): JobPlace[] {
  const out: JobPlace[] = [];
  for (const sel of LOCATION_SELECTORS) {
    for (const el of Array.from(doc.querySelectorAll(sel)).slice(0, 4)) {
      if (el.closest("form")) continue; // a form's own location field is the applicant's
      const p = placeOfText(el.textContent || "");
      if (p) out.push(p);
    }
    if (out.length) break;
  }
  if (out.length > 0) return out;
  const labelled = labelledPlaces(doc);
  return labelled.length > 0 ? labelled : titlePlaces(doc);
}

/** The line right after the job title, when every part of it is a place:
 *  "Bellevue, Washington; Mountain View, California; San Francisco,
 *  California" (Databricks' career site, live 2026-10-05). Its own text
 *  only, not the Apply button inside it. */
function titlePlaces(doc: Document): JobPlace[] {
  const h1 = doc.querySelector("h1");
  if (!h1 || h1.closest("form")) return [];
  let line = h1.nextElementSibling;
  for (let i = 0; i < 2 && line; i++, line = line.nextElementSibling) {
    const own = Array.from(line.childNodes)
      .filter((c) => c.nodeType === Node.TEXT_NODE)
      .map((c) => c.textContent ?? "")
      .join(" ")
      .replace(/\s+/g, " ")
      .trim() || (line.children.length === 0 ? (line.textContent ?? "").trim() : "");
    if (!own || own.length > 200) continue;
    const parts = own.split(/\s*[;|•·]\s*/).filter(Boolean);
    const places = parts.map(placeOfText);
    if (places.length > 0 && places.every((p) => p !== null)) return places as JobPlace[];
  }
  return [];
}

const LOCATION_TERM = /^(job |work |office )?locations?\s*:?$/i;

/** A "Location" label and the value after it, outside any form:
 *  `<strong>Location</strong><p>Cary, United States</p>` (Epic Games' career
 *  site, live 2026-10-05), `<dt>Location:</dt><dd>Toronto, ON</dd>`. */
function labelledPlaces(doc: Document): JobPlace[] {
  const out: JobPlace[] = [];
  for (const text of labelledValues(doc)) {
    if (out.length >= 4) break;
    const p = placeOfText(text);
    if (p) out.push(p);
  }
  return out;
}

/** The text after each "Location" label outside any form. */
function labelledValues(doc: Document): string[] {
  const out: string[] = [];
  for (const term of Array.from(doc.querySelectorAll("strong, b, dt, th, h2, h3, h4, h5, h6, span, div, label"))) {
    if (out.length >= 8) break;
    if (term.children.length > 0 || !LOCATION_TERM.test((term.textContent || "").trim())) continue;
    if (term.closest("form")) continue; // the applicant's own location box
    const value = term.nextElementSibling;
    if (value && !value.closest("form")) out.push(value.textContent || "");
  }
  return out;
}

/**
 * Workday puts the job's location in its URL: `/job/<Location>/<Title>_<Req>`
 * ("Cambridge-MA", "Dayton-Minnesota-USA-French-Lake", "Texas---Houston-
 * Corporate-Office", "Toronto-ON"), and keeps it on every application step,
 * which otherwise shows no location at all. Only unambiguous evidence counts:
 * a country name, a full state/province name, or an UPPERCASE two-letter code
 * ("MA", "ON"; a lowercase "in"/"or" is a word, not Indiana/Oregon).
 */
export function countryFromWorkdayUrl(href: string): string | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (!/myworkday(jobs|site)\.com$/i.test(url.hostname)) return null;
  const m = /\/job\/([^/]+)\//.exec(url.pathname);
  if (!m) return null;
  const tokens = decodeURIComponent(m[1]).split(/[-_,\s]+/).filter(Boolean);
  const found = new Set<string>();
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    const pair = i + 1 < tokens.length ? `${t} ${tokens[i + 1]}` : "";
    const triple = i + 2 < tokens.length ? `${t} ${tokens[i + 1]} ${tokens[i + 2]}` : "";
    for (const phrase of [triple, pair, t]) {
      if (!phrase) continue;
      const c = phrase.length === 2 ? null : countryFromName(phrase);
      if (c) found.add(c.code);
      const isCode = /^[A-Z]{2}$/.test(phrase);
      if (isCode || phrase.length > 3) {
        const r = regionFromText(phrase);
        if (r && (isCode || r.name.toLowerCase() === phrase.toLowerCase())) found.add(r.country);
      }
    }
  }
  return found.size === 1 ? [...found][0] : null;
}

const cityKey = (city: string): string =>
  city.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z]+/g, " ").trim();

/** The job's country and city: each only when every signal agrees on one. */
export function detectJobPlace(doc: Document = document): JobPlace {
  try {
    let places = jsonLdPlaces(doc);
    if (places.length === 0) places = elementPlaces(doc);
    const countries = [...new Set(places.map((p) => p.country).filter((c): c is string => Boolean(c)))];
    const fromUrl = countryFromWorkdayUrl(doc.location?.href ?? "");
    const country = fromUrl ?? (countries.length === 1 ? countries[0] : null);
    // One city: every place names it, and it lies in the job's country.
    const keys = new Set(places.map((p) => (p.city ? cityKey(p.city) : "")));
    const city =
      country && places.length > 0 && keys.size === 1 && !keys.has("") && places.every((p) => p.country === country)
        ? places[0].city
        : null;
    const listed = listedPlaces(doc);
    return { country, city, ...(listed.length > 0 ? { places: listed } : {}) };
  } catch {
    return { country: null, city: null };
  }
}

export function detectJobCountry(doc: Document = document): string | null {
  return detectJobPlace(doc).country;
}

/** The hiring company, or "" when the page only names its ATS vendor. */
export function sanitizeCompany(name: string): string {
  const n = (name || "").trim();
  return ATS_NAMES.test(n) ? "" : n;
}
