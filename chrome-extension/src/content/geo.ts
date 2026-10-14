/**
 * Geographic reference data for deterministic inference: countries (with the
 * spellings forms actually use), US states, Canadian provinces, postal-code
 * shapes, and continents.
 *
 * Deliberately small and exact. Every lookup either recognizes a place or says
 * it does not; nothing here guesses. A location the tables do not know simply
 * yields no fact, and the field it would have answered is left for the user.
 */

export interface Country {
  /** ISO 3166-1 alpha-2. */
  code: string;
  /** The name most forms list. */
  name: string;
  /** Other spellings, abbreviations and demonyms (lowercase, no punctuation). */
  aliases: string[];
  continent: Continent;
}

export type Continent = "North America" | "South America" | "Europe" | "Asia" | "Africa" | "Oceania";

export const COUNTRIES: Country[] = [
  { code: "US", name: "United States", continent: "North America", aliases: ["us", "usa", "u s", "u s a", "united states of america", "america", "american", "the united states", "the us", "the usa", "states"] },
  { code: "CA", name: "Canada", continent: "North America", aliases: ["can", "canadian"] },
  { code: "MX", name: "Mexico", continent: "North America", aliases: ["mex", "mexican", "mexico"] },
  { code: "GB", name: "United Kingdom", continent: "Europe", aliases: ["gbr", "uk", "u k", "great britain", "britain", "british", "england", "scotland", "wales", "northern ireland", "the uk", "the united kingdom"] },
  { code: "IE", name: "Ireland", continent: "Europe", aliases: ["irl", "irish", "republic of ireland"] },
  { code: "FR", name: "France", continent: "Europe", aliases: ["fra", "french republic"] },
  { code: "DE", name: "Germany", continent: "Europe", aliases: ["deu", "german", "deutschland"] },
  { code: "NL", name: "Netherlands", continent: "Europe", aliases: ["nld", "the netherlands", "holland", "dutch"] },
  { code: "BE", name: "Belgium", continent: "Europe", aliases: ["bel", "belgian"] },
  { code: "ES", name: "Spain", continent: "Europe", aliases: ["esp", "spanish", "espana"] },
  { code: "PT", name: "Portugal", continent: "Europe", aliases: ["prt", "portuguese"] },
  { code: "IT", name: "Italy", continent: "Europe", aliases: ["ita", "italian", "italia"] },
  { code: "CH", name: "Switzerland", continent: "Europe", aliases: ["che", "swiss"] },
  { code: "AT", name: "Austria", continent: "Europe", aliases: ["aut", "austrian"] },
  { code: "SE", name: "Sweden", continent: "Europe", aliases: ["swe", "swedish"] },
  { code: "NO", name: "Norway", continent: "Europe", aliases: ["nor", "norwegian"] },
  { code: "DK", name: "Denmark", continent: "Europe", aliases: ["dnk", "danish"] },
  { code: "FI", name: "Finland", continent: "Europe", aliases: ["fin", "finnish"] },
  { code: "PL", name: "Poland", continent: "Europe", aliases: ["pol", "polish"] },
  { code: "CZ", name: "Czech Republic", continent: "Europe", aliases: ["czechia", "czech"] },
  { code: "RO", name: "Romania", continent: "Europe", aliases: ["romanian"] },
  { code: "GR", name: "Greece", continent: "Europe", aliases: ["greek"] },
  { code: "UA", name: "Ukraine", continent: "Europe", aliases: ["ukrainian"] },
  { code: "TR", name: "Turkey", continent: "Asia", aliases: ["turkiye", "turkish"] },
  { code: "IL", name: "Israel", continent: "Asia", aliases: ["isr", "israeli"] },
  { code: "AE", name: "United Arab Emirates", continent: "Asia", aliases: ["uae", "u a e", "emirates"] },
  { code: "SA", name: "Saudi Arabia", continent: "Asia", aliases: ["saudi", "ksa"] },
  { code: "IN", name: "India", continent: "Asia", aliases: ["ind", "indian", "bharat"] },
  { code: "PK", name: "Pakistan", continent: "Asia", aliases: ["pak", "pakistani"] },
  { code: "BD", name: "Bangladesh", continent: "Asia", aliases: ["bangladeshi"] },
  { code: "LK", name: "Sri Lanka", continent: "Asia", aliases: ["sri lankan"] },
  { code: "CN", name: "China", continent: "Asia", aliases: ["chn", "chinese", "prc", "people s republic of china", "mainland china"] },
  { code: "HK", name: "Hong Kong", continent: "Asia", aliases: ["hkg", ] },
  { code: "TW", name: "Taiwan", continent: "Asia", aliases: ["taiwanese"] },
  { code: "JP", name: "Japan", continent: "Asia", aliases: ["jpn", "japanese"] },
  { code: "KR", name: "South Korea", continent: "Asia", aliases: ["kor", "korea", "republic of korea", "korean"] },
  { code: "SG", name: "Singapore", continent: "Asia", aliases: ["sgp", "singaporean"] },
  { code: "MY", name: "Malaysia", continent: "Asia", aliases: ["malaysian"] },
  { code: "PH", name: "Philippines", continent: "Asia", aliases: ["phl", "the philippines", "filipino"] },
  { code: "VN", name: "Vietnam", continent: "Asia", aliases: ["viet nam", "vietnamese"] },
  { code: "TH", name: "Thailand", continent: "Asia", aliases: ["thai"] },
  { code: "ID", name: "Indonesia", continent: "Asia", aliases: ["indonesian"] },
  { code: "AU", name: "Australia", continent: "Oceania", aliases: ["aus", "australian"] },
  { code: "NZ", name: "New Zealand", continent: "Oceania", aliases: ["nzl", "kiwi"] },
  { code: "BR", name: "Brazil", continent: "South America", aliases: ["bra", "brasil", "brazilian"] },
  { code: "AR", name: "Argentina", continent: "South America", aliases: ["arg", "argentinian", "argentine"] },
  { code: "CL", name: "Chile", continent: "South America", aliases: ["chilean"] },
  { code: "CO", name: "Colombia", continent: "South America", aliases: ["colombian"] },
  { code: "PE", name: "Peru", continent: "South America", aliases: ["peruvian"] },
  { code: "NG", name: "Nigeria", continent: "Africa", aliases: ["nga", "nigerian"] },
  { code: "KE", name: "Kenya", continent: "Africa", aliases: ["kenyan"] },
  { code: "ZA", name: "South Africa", continent: "Africa", aliases: ["zaf", "south african"] },
  { code: "EG", name: "Egypt", continent: "Africa", aliases: ["egyptian"] },
  { code: "MA", name: "Morocco", continent: "Africa", aliases: ["moroccan"] },
  { code: "GH", name: "Ghana", continent: "Africa", aliases: ["ghanaian"] },
];

/**
 * A lookup table with no prototype: a user's string ("constructor",
 * "__proto__", "toString") indexes it as an unknown key, never as an inherited
 * Object.prototype member (a city named "constructor" crashed the location
 * facts, owner decisions 2026-10-13).
 */
export function ownKeys<T>(entries: Record<string, T>): Record<string, T> {
  return Object.assign(Object.create(null) as Record<string, T>, entries);
}

/** `table[key]` only when the table itself holds that key. */
export function ownValue<T>(table: Record<string, T>, key: string): T | null {
  return Object.hasOwn(table, key) ? table[key] : null;
}

/** International dialing code (digits after "+") of each country above. A
 *  phone picker reads back "+1" for the "Canada" it was given (Greenhouse). */
export const DIAL_CODES: Record<string, string> = ownKeys({
  US: "1", CA: "1", MX: "52", GB: "44", IE: "353", FR: "33", DE: "49", NL: "31", BE: "32", ES: "34",
  PT: "351", IT: "39", CH: "41", AT: "43", SE: "46", NO: "47", DK: "45", FI: "358", PL: "48", CZ: "420",
  RO: "40", GR: "30", UA: "380", TR: "90", IL: "972", AE: "971", SA: "966", IN: "91", PK: "92", BD: "880",
  LK: "94", CN: "86", HK: "852", TW: "886", JP: "81", KR: "82", SG: "65", MY: "60", PH: "63", VN: "84",
  TH: "66", ID: "62", AU: "61", NZ: "64", BR: "55", AR: "54", CL: "56", CO: "57", PE: "51", NG: "234",
  KE: "254", ZA: "27", EG: "20", MA: "212", GH: "233",
});

export interface Region {
  code: string;
  name: string;
  country: "US" | "CA";
  /** Alternate spellings (lowercase, accents stripped), e.g. French names. */
  aliases?: string[];
}

export const CA_PROVINCES: Region[] = [
  { code: "AB", name: "Alberta", country: "CA" },
  { code: "BC", name: "British Columbia", country: "CA", aliases: ["colombie britannique"] },
  { code: "MB", name: "Manitoba", country: "CA" },
  { code: "NB", name: "New Brunswick", country: "CA", aliases: ["nouveau brunswick"] },
  { code: "NL", name: "Newfoundland and Labrador", country: "CA", aliases: ["newfoundland", "terre neuve et labrador"] },
  { code: "NS", name: "Nova Scotia", country: "CA", aliases: ["nouvelle ecosse"] },
  { code: "NT", name: "Northwest Territories", country: "CA", aliases: ["territoires du nord ouest"] },
  { code: "NU", name: "Nunavut", country: "CA" },
  { code: "ON", name: "Ontario", country: "CA" },
  { code: "PE", name: "Prince Edward Island", country: "CA", aliases: ["pei", "ile du prince edouard"] },
  { code: "QC", name: "Quebec", country: "CA", aliases: ["pq", "province de quebec"] },
  { code: "SK", name: "Saskatchewan", country: "CA" },
  { code: "YT", name: "Yukon", country: "CA", aliases: ["yukon territory"] },
];

export const US_STATES: Region[] = [
  ["AL", "Alabama"], ["AK", "Alaska"], ["AZ", "Arizona"], ["AR", "Arkansas"], ["CA", "California"],
  ["CO", "Colorado"], ["CT", "Connecticut"], ["DE", "Delaware"], ["FL", "Florida"], ["GA", "Georgia"],
  ["HI", "Hawaii"], ["ID", "Idaho"], ["IL", "Illinois"], ["IN", "Indiana"], ["IA", "Iowa"],
  ["KS", "Kansas"], ["KY", "Kentucky"], ["LA", "Louisiana"], ["ME", "Maine"], ["MD", "Maryland"],
  ["MA", "Massachusetts"], ["MI", "Michigan"], ["MN", "Minnesota"], ["MS", "Mississippi"], ["MO", "Missouri"],
  ["MT", "Montana"], ["NE", "Nebraska"], ["NV", "Nevada"], ["NH", "New Hampshire"], ["NJ", "New Jersey"],
  ["NM", "New Mexico"], ["NY", "New York"], ["NC", "North Carolina"], ["ND", "North Dakota"], ["OH", "Ohio"],
  ["OK", "Oklahoma"], ["OR", "Oregon"], ["PA", "Pennsylvania"], ["RI", "Rhode Island"], ["SC", "South Carolina"],
  ["SD", "South Dakota"], ["TN", "Tennessee"], ["TX", "Texas"], ["UT", "Utah"], ["VT", "Vermont"],
  ["VA", "Virginia"], ["WA", "Washington"], ["WV", "West Virginia"], ["WI", "Wisconsin"], ["WY", "Wyoming"],
  ["DC", "District of Columbia"], ["PR", "Puerto Rico"],
].map(([code, name]) => ({ code, name, country: "US" as const, aliases: code === "DC" ? ["washington dc", "washington d c"] : undefined }));

/** Lowercase, accents stripped, punctuation → space. */
export function geoNorm(text: string): string {
  return (text || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

const COUNTRY_BY_KEY = new Map<string, Country>();
for (const c of COUNTRIES) {
  COUNTRY_BY_KEY.set(geoNorm(c.name), c);
  COUNTRY_BY_KEY.set(c.code.toLowerCase(), c);
  for (const a of c.aliases) COUNTRY_BY_KEY.set(geoNorm(a), c);
}

/**
 * A country named by `text` as a WHOLE ("Canada", "USA", "U.S.", "Canadian").
 * Two-letter ISO codes other than US/UK are NOT accepted here: "CA", "IN", "DE"
 * are far more often a US state in a location string than a country code.
 */
export function countryFromName(text: string): Country | null {
  const key = geoNorm(text);
  if (!key) return null;
  if (key.length === 2 && key !== "us" && key !== "uk") return null;
  return COUNTRY_BY_KEY.get(key) ?? null;
}

export function countryByCode(code: string): Country | null {
  const c = COUNTRIES.find((x) => x.code === code.toUpperCase());
  return c ?? null;
}

const REGIONS = [...CA_PROVINCES, ...US_STATES];

/** A US state / Canadian province named or coded by `text` as a whole. When a
 *  code exists in both countries it cannot (none do today), `prefer` decides. */
export function regionFromText(text: string, prefer?: "US" | "CA"): Region | null {
  const key = geoNorm(text);
  if (!key) return null;
  const hits = REGIONS.filter(
    (r) => r.code.toLowerCase() === key || geoNorm(r.name) === key || (r.aliases ?? []).some((a) => geoNorm(a) === key)
  );
  if (hits.length === 0) return null;
  if (hits.length === 1) return hits[0];
  return hits.find((h) => h.country === prefer) ?? null;
}

export function regionsOf(country: "US" | "CA"): Region[] {
  return country === "US" ? US_STATES : CA_PROVINCES;
}

/** Postal / ZIP code shapes, each pinning the country it belongs to. */
const CA_POSTAL = /\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z])\s?(\d[ABCEGHJ-NPRSTV-Z]\d)\b/i;
const US_ZIP = /\b(\d{5})(?:-(\d{4}))?\b/g;
const UK_POSTCODE = /\b([A-Z]{1,2}\d[A-Z\d]?)\s(\d[A-Z]{2})\b/i;

export function findPostalCode(text: string): { code: string; country: string } | null {
  const ca = CA_POSTAL.exec(text);
  if (ca) return { code: `${ca[1]} ${ca[2]}`.toUpperCase(), country: "CA" };
  const uk = UK_POSTCODE.exec(text);
  if (uk) return { code: `${uk[1]} ${uk[2]}`.toUpperCase(), country: "GB" };
  // The LAST five-digit run: a US address leads with its street number
  // ("12345 Main St, Springfield, IL 62701"), the ZIP always comes last.
  const zips = [...text.matchAll(US_ZIP)];
  const us = zips[zips.length - 1];
  if (us) return { code: us[2] ? `${us[1]}-${us[2]}` : us[1], country: "US" };
  return null;
}

/**
 * Cities common enough in our users' profiles that a bare "Toronto" is
 * evidence of its country. Evidence, not proof (there is a London, Ontario):
 * facts derived from this list are capped at MEDIUM confidence and never fill.
 */
const CITY_COUNTRY: Record<string, string> = ownKeys({
  toronto: "CA", montreal: "CA", vancouver: "CA", ottawa: "CA", calgary: "CA", edmonton: "CA",
  winnipeg: "CA", mississauga: "CA", brampton: "CA", hamilton: "CA", kitchener: "CA", waterloo: "CA",
  markham: "CA", "quebec city": "CA", halifax: "CA", victoria: "CA", saskatoon: "CA", regina: "CA",
  burnaby: "CA", surrey: "CA", laval: "CA", gatineau: "CA", oakville: "CA", guelph: "CA",
  "new york": "US", "new york city": "US", nyc: "US", "san francisco": "US", seattle: "US", boston: "US",
  chicago: "US", austin: "US", "los angeles": "US", "san jose": "US", "palo alto": "US", denver: "US",
  atlanta: "US", miami: "US", dallas: "US", houston: "US", "washington dc": "US", philadelphia: "US",
});

export function countryHintForCity(city: string): string | null {
  return ownValue(CITY_COUNTRY, geoNorm(city));
}

/**
 * The state or province of well-known office cities a posting names alone
 * ("our Austin office", Cloudflare, question bank 2026-10-05): the best-known
 * city of the name. Used only to tell that an office is somewhere an applicant
 * who will not move does not live.
 */
const CITY_REGION: Record<string, string> = ownKeys({
  "new york": "NY", "new york city": "NY", nyc: "NY", "san francisco": "CA", seattle: "WA", boston: "MA",
  chicago: "IL", austin: "TX", "los angeles": "CA", "san jose": "CA", "palo alto": "CA", "mountain view": "CA",
  "menlo park": "CA", sunnyvale: "CA", "san diego": "CA", denver: "CO", atlanta: "GA", miami: "FL",
  dallas: "TX", houston: "TX", philadelphia: "PA", "washington dc": "DC", pittsburgh: "PA",
  "salt lake city": "UT", phoenix: "AZ", minneapolis: "MN", nashville: "TN", raleigh: "NC", detroit: "MI",
  redmond: "WA", bellevue: "WA",
  toronto: "ON", montreal: "QC", vancouver: "BC", ottawa: "ON", calgary: "AB", edmonton: "AB",
  mississauga: "ON", markham: "ON", "quebec city": "QC",
});

/** "TX" for "Austin": a well-known office city's state or province code. */
export function regionHintForCity(city: string): string | null {
  return ownValue(CITY_REGION, geoNorm(city));
}

/** US states (and DC) and Canadian provinces that share a border. */
const BORDERS = new Set(
  (
    "AL-FL AL-GA AL-MS AL-TN AZ-CA AZ-CO AZ-NM AZ-NV AZ-UT AR-LA AR-MO AR-MS AR-OK AR-TN AR-TX CA-NV CA-OR " +
    "CO-KS CO-NE CO-NM CO-OK CO-UT CO-WY CT-MA CT-NY CT-RI DE-MD DE-NJ DE-PA DC-MD DC-VA FL-GA GA-NC GA-SC " +
    "GA-TN ID-MT ID-NV ID-OR ID-UT ID-WA ID-WY IL-IN IL-IA IL-KY IL-MO IL-WI IN-KY IN-MI IN-OH IA-MN IA-MO " +
    "IA-NE IA-SD IA-WI KS-MO KS-NE KS-OK KY-MO KY-OH KY-TN KY-VA KY-WV LA-MS LA-TX ME-NH MD-PA MD-VA MD-WV " +
    "MA-NH MA-NY MA-RI MA-VT MI-OH MI-WI MN-ND MN-SD MN-WI MS-TN MO-NE MO-OK MO-TN MT-ND MT-SD MT-WY NE-SD " +
    "NE-WY NV-OR NV-UT NH-VT NJ-NY NJ-PA NM-OK NM-TX NM-UT NY-PA NY-VT NC-SC NC-TN NC-VA ND-SD OH-PA OH-WV " +
    "OK-TX OR-WA PA-WV SD-WY TN-VA UT-WY VA-WV " +
    "BC-AB AB-SK SK-MB MB-ON ON-QC QC-NB QC-NL NB-NS NB-PE NS-PE BC-YT BC-NT AB-NT SK-NT MB-NU YT-NT NT-NU"
  )
    .split(" ")
    .flatMap((pair) => {
      const [a, b] = pair.split("-");
      return [`${a}-${b}`, `${b}-${a}`];
    })
);

/** Two states or provinces that border each other: a metro can straddle the
 *  line (Jersey City for New York, Gatineau for Ottawa), so a neighbour is
 *  never "elsewhere" on its own. Codes, as in US_STATES / CA_PROVINCES. */
export function regionsBorder(a: string, b: string): boolean {
  return BORDERS.has(`${a.toUpperCase()}-${b.toUpperCase()}`);
}

/** The well-known cities above that share a metro area. */
const METROS: string[][] = [
  ["san francisco", "san jose", "palo alto", "mountain view", "menlo park", "sunnyvale", "bay area"],
  ["seattle", "redmond", "bellevue"],
  ["toronto", "mississauga", "markham", "gta"],
  ["new york", "new york city", "nyc"],
];

/**
 * Whether two places are the same metro area, when both are cities this file
 * knows: true for the same city or metro (San Jose and Palo Alto), false for
 * two known cities apart (Toronto and Montreal: Ontario borders Quebec, yet
 * no one commutes between them), null when either is unknown (Jersey City).
 */
export function sameMetro(a: string, b: string): boolean | null {
  const x = geoNorm(a);
  const y = geoNorm(b);
  if (x === y) return true;
  const known = (c: string): boolean => Object.hasOwn(CITY_REGION, c) || METROS.some((m) => m.includes(c));
  if (!known(x) || !known(y)) return null;
  return METROS.some((m) => m.includes(x) && m.includes(y));
}

/** The cities above, longest first, for scanning question text. */
export const REGION_CITIES: string[] = Object.keys(CITY_REGION).sort((a, b) => b.length - a.length);

/** The cities above, longest first, for scanning question text. */
export const KNOWN_CITIES: string[] = Object.keys(CITY_COUNTRY).sort((a, b) => b.length - a.length);
