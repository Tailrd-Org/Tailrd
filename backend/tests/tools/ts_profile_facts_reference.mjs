// Runs the extension's REAL profile-facts code over a corpus and prints what it returned, as JSON.
//
//   node --experimental-strip-types --no-warnings ts_profile_facts_reference.mjs <corpus.json> <content dir>
//
// <content dir> is chrome-extension/src/content. profileFacts.ts and geo.ts are imported from there,
// in place: Node strips the types itself and ts_extension_hooks.mjs resolves their extensionless
// imports, so nothing is copied or built. Driven by profile_facts_parity.py, which documents the
// case kinds. Output: { "results": [{ok: ...} | {error: "..."}, ...], "tables": {...} }.
import { readFileSync } from "node:fs";
import { register } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

register("./ts_extension_hooks.mjs", import.meta.url);

const [corpusPath, contentDir] = process.argv.slice(2);
const facts = await import(pathToFileURL(join(contentDir, "profileFacts.ts")).href);
const geo = await import(pathToFileURL(join(contentDir, "geo.ts")).href);

const country = (c) => (c ? { code: c.code, name: c.name } : null);
const region = (r) => (r ? { code: r.code, name: r.name, country: r.country } : null);

function normFact(f, kind) {
  if (!f) return null;
  let value = f.value;
  if (kind === "country") value = country(value);
  else if (kind === "region") value = region(value);
  return { value, confidence: f.confidence, source: f.source };
}

function normParsed(p) {
  const out = {};
  if (p.street !== undefined) out.street = p.street;
  if (p.city !== undefined) out.city = p.city;
  if (p.region !== undefined) out.region = region(p.region);
  if (p.postal !== undefined) out.postal = { code: p.postal.code, country: p.postal.country };
  if (p.country !== undefined) out.country = country(p.country);
  return out;
}

function run(c) {
  switch (c.kind) {
    case "polarity":
      return facts.polarityOf(c.text);
    case "countriesIn":
      return facts.countriesIn(c.text).map((x) => x.code);
    case "parseAddress":
      return normParsed(facts.parseAddress(c.text));
    case "location": {
      const f = facts.locationFacts(c.profile);
      return {
        street: normFact(f.street),
        city: normFact(f.city),
        region: normFact(f.region, "region"),
        postalCode: normFact(f.postalCode),
        country: normFact(f.country, "country"),
      };
    }
    case "workAuth": {
      const a = facts.workAuthFacts(c.profile);
      return {
        byCountry: [...a.byCountry.entries()],
        statedSponsorship: a.statedSponsorship,
        unscopedAuthorized: a.unscopedAuthorized,
        queries: (c.queries ?? []).map(([countryCode, residence]) => ({
          q: [countryCode, residence],
          authorized: normFact(facts.authorizedIn(a, countryCode, residence)),
          sponsorship: normFact(facts.needsSponsorshipIn(a, countryCode, residence)),
        })),
      };
    }
    default:
      throw new Error("unknown case kind " + c.kind);
  }
}

const corpus = JSON.parse(readFileSync(corpusPath, "utf8"));
const results = corpus.map((c) => {
  try {
    return { ok: run(c) };
  } catch (e) {
    return { error: String(e && e.message ? e.message : e) };
  }
});

// The geo tables. COUNTRIES, CA_PROVINCES and US_STATES are exported; CITY_COUNTRY and
// FREE_MOVEMENT are private consts, so their literals are lifted out of the source text.
function lift(sourceText, pattern, what) {
  const match = sourceText.match(pattern);
  if (!match) {
    return {
      error: `cannot find ${what} in the extension source: its layout changed, update ts_profile_facts_reference.mjs`,
    };
  }
  return { value: new Function("return " + match[1])() };
}
const geoSource = readFileSync(join(contentDir, "geo.ts"), "utf8");
const factsSource = readFileSync(join(contentDir, "profileFacts.ts"), "utf8");
const tables = {
  COUNTRIES: { value: geo.COUNTRIES },
  CA_PROVINCES: { value: geo.CA_PROVINCES },
  US_STATES: { value: geo.US_STATES },
  CITY_COUNTRY: lift(
    geoSource,
    /const CITY_COUNTRY: Record<string, string> = (\{[\s\S]*?\n\});/,
    "CITY_COUNTRY in geo.ts",
  ),
  FREE_MOVEMENT: lift(
    factsSource,
    /const FREE_MOVEMENT = new Set\((\[[\s\S]*?\])\);/,
    "FREE_MOVEMENT in profileFacts.ts",
  ),
};

process.stdout.write(JSON.stringify({ results, tables }));
