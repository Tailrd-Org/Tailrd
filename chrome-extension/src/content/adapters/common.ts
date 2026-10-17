// chrome-extension/src/content/adapters/common.ts
/**
 * Breadth coverage for the ATS platforms whose forms the generic pipeline
 * already fills well, but which we want to formally *recognize* so the adapter
 * seams (classify / advance / future fill ops) are available on them.
 *
 * Each entry is a thin `SiteAdapter`: an anchored host match plus attribute-based
 * classification (social-URL detection everywhere, name-attribute detection for
 * the ATS that namespace their fields). Greenhouse and Workday keep their own
 * hand-tuned modules (deeper quirks); everything else is declared in one table
 * here so a new ATS is a one-line addition, not a new file.
 *
 * Host regexes are anchored with `(^|\.)…$` so `notgreenhouse.io.evil.com` and
 * similar look-alikes can never match.
 */
import { ADAPTERS } from "./registry";
import {
  advanceBySelectors,
  classifyByAttr,
  NAME_ATTR_RULES,
  SOCIAL_URL_RULES,
  type AttrRule,
} from "./shared";
import type { SiteAdapter } from "./types";

interface AtsSpec {
  id: string;
  /** Anchored host test: the sole detection signal. */
  host: RegExp;
  /** Optional extra url gate (rarely needed; host is usually enough). */
  url?: RegExp;
  /** ATS-specific classify rules, tried before the shared social-URL rules. */
  rules?: readonly AttrRule[];
  /** Exact advance/next selectors for multi-step forms (terminal-checked by the caller). */
  advance?: readonly string[];
  /** Classification confidence for this adapter's rules (default 0.95). */
  confidence?: number;
}

/**
 * Panel labels for the thin adapters, keyed by adapter id. Hand-tuned modules
 * (greenhouse/workday/lever) set their own; anything unmapped shows its id.
 */
const LABEL_BY_ID: Record<string, string> = {
  // existing thin adapters
  bamboohr: "BambooHR", breezy: "Breezy", ashby: "Ashby", workable: "Workable",
  smartrecruiters: "SmartRecruiters", jobvite: "Jobvite", rippling: "Rippling",
  bullhorn: "Bullhorn", icims: "iCIMS", taleo: "Taleo", adp: "ADP",
  successfactors: "SuccessFactors", oraclecloud: "Oracle Cloud", dayforce: "Dayforce",
  ukg: "UKG Pro", jazzhr: "JazzHR", paylocity: "Paylocity", avature: "Avature",
  phenom: "Phenom", teamtailor: "Teamtailor", recruitee: "Recruitee",
  personio: "Personio", eightfold: "Eightfold", clearcompany: "ClearCompany",
  paycom: "Paycom", brassring: "BrassRing",
  // broader Jobright-parity vendors
  kula: "Kula", dover: "Dover", zohorecruit: "Zoho Recruit", gem: "Gem",
  hiringthing: "HiringThing", catsone: "CATS", ripplehire: "RippleHire",
  careerspage: "CareersPage", careerplug: "CareerPlug", isolved: "isolved",
  jobdiva: "JobDiva", gohire: "GoHire", trakstar: "Trakstar", freshteam: "Freshteam",
  pinpointhq: "Pinpoint", trinethire: "TriNet Hire", jobscore: "JobScore",
  comeet: "Comeet", polymer: "Polymer", recruiterflow: "Recruiterflow",
};

/** Build a thin adapter from a spec: host match + attribute classification. */
export function buildAtsAdapter(spec: AtsSpec): SiteAdapter {
  const rules: readonly AttrRule[] = [...(spec.rules ?? []), ...SOCIAL_URL_RULES];
  const adapter: SiteAdapter = {
    id: spec.id,
    label: LABEL_BY_ID[spec.id] ?? spec.id,
    match: (host, url) => spec.host.test(host) && (!spec.url || spec.url.test(url)),
    classify: (ctx) => classifyByAttr(ctx.el, rules, spec.confidence ?? 0.95),
  };
  if (spec.advance) {
    const selectors = spec.advance;
    adapter.advanceButton = (scope) => advanceBySelectors(scope, selectors);
  }
  return adapter;
}

/**
 * The registry. Ordered by the coverage tier in docs/ats-coverage.md, then the
 * broader set Jobright ships. First-match-wins, but hosts are disjoint so order
 * only affects readability.
 */
export const COMMON_ATS: readonly AtsSpec[] = [
  // ---- Easy tier -----------------------------------------------------------
  // Lever has its own hand-tuned module (lever.ts): the org rule below plus the
  // location-typeahead fill op live there now.
  { id: "bamboohr", host: /(^|\.)bamboohr\.com$/i },
  { id: "breezy", host: /(^|\.)breezy\.hr$/i },

  // ---- Medium tier ---------------------------------------------------------
  { id: "ashby", host: /(^|\.)ashbyhq\.com$/i },
  { id: "workable", host: /(^|\.)workable\.com$/i },
  { id: "smartrecruiters", host: /(^|\.)smartrecruiters\.com$/i },
  { id: "jobvite", host: /(^|\.)jobvite\.com$/i },
  { id: "rippling", host: /(^|\.)(rippling|rippling-ats)\.com$/i },
  { id: "bullhorn", host: /(^|\.)(bullhornstaffing|bullhorn|talentrackr)\.com$/i },

  // ---- Hard tier (Workday has its own module) ------------------------------
  // The candidate login's submit is its Next, whatever it says: Schwab's reads
  // "I Acknowledge the Privacy Notice" (live 2026-10-17).
  { id: "icims", host: /(^|\.)icims\.com$/i, advance: ["form.iCIMS_SignIn #enterEmailSubmitButton"] },
  { id: "taleo", host: /(^|\.)taleo\.net$/i },
  { id: "adp", host: /(^|\.)adp\.com$/i },
  { id: "successfactors", host: /(^|\.)(successfactors\.(com|eu)|sapsf\.(com|eu))$/i },

  // ---- Broader Jobright-parity set -----------------------------------------
  { id: "oraclecloud", host: /(^|\.)oraclecloud\.com$/i },
  { id: "dayforce", host: /(^|\.)(dayforcehcm|dayforce)\.com$/i },
  { id: "ukg", host: /(^|\.)(ultipro\.(com|ca)|ukg\.(com|net))$/i },
  { id: "jazzhr", host: /(^|\.)(applytojob\.com|jazz\.co)$/i, rules: NAME_ATTR_RULES },
  { id: "paylocity", host: /(^|\.)paylocity\.com$/i },
  { id: "avature", host: /(^|\.)avature\.net$/i },
  { id: "phenom", host: /(^|\.)phenompeople\.com$/i },
  { id: "teamtailor", host: /(^|\.)teamtailor\.com$/i, rules: NAME_ATTR_RULES },
  { id: "recruitee", host: /(^|\.)recruitee\.com$/i, rules: NAME_ATTR_RULES },
  { id: "personio", host: /(^|\.)personio\.(de|com|es|nl|fr)$/i },
  { id: "eightfold", host: /(^|\.)eightfold\.ai$/i },
  { id: "clearcompany", host: /(^|\.)clearcompany\.com$/i },
  { id: "paycom", host: /(^|\.)paycomonline\.(com|net)$/i },
  { id: "brassring", host: /(^|\.)(brassring|kenexa)\.com$/i },

  // ---- Full Jobright-parity vendor set (host suffixes ported from the site
  //      registry; company-specific *portals* are recognized precisely by
  //      detectSite instead, since a bare host match would be too broad). -----
  { id: "kula", host: /(^|\.)(careers\.kula\.ai)$/i },
  { id: "dover", host: /(^|\.)(dover\.com)$/i },
  { id: "zohorecruit", host: /(^|\.)(zohorecruit\.com|zohorecruit\.ca|zohorecruit\.eu)$/i },
  { id: "gem", host: /(^|\.)(jobs\.gem\.com)$/i },
  { id: "hiringthing", host: /(^|\.)(hiringthing\.com|oasisrecruit\.com|elevate-ats\.com|prismhr-hire\.com|gnahiring\.com)$/i },
  { id: "catsone", host: /(^|\.)(catsone\.com)$/i },
  { id: "ripplehire", host: /(^|\.)(ripplehire\.com)$/i },
  { id: "careerspage", host: /(^|\.)(careers-page\.com)$/i },
  { id: "careerplug", host: /(^|\.)(careerplug\.com|sfagentjobs\.com|sfagentcareers\.com|apscareerportal\.com)$/i },
  { id: "isolved", host: /(^|\.)(isolvedhire\.com)$/i },
  { id: "jobdiva", host: /(^|\.)(jobdiva\.com)$/i },
  { id: "gohire", host: /(^|\.)(app\.gohire\.io|jobs\.gohire\.io)$/i },
  { id: "trakstar", host: /(^|\.)(hire\.trakstar\.com)$/i },
  { id: "freshteam", host: /(^|\.)(freshteam\.com)$/i },
  { id: "pinpointhq", host: /(^|\.)(pinpointhq\.com)$/i },
  { id: "trinethire", host: /(^|\.)(app\.trinethire\.com)$/i },
  { id: "jobscore", host: /(^|\.)(jobscore\.com|careers\.jobscore\.com)$/i },
  { id: "comeet", host: /(^|\.)(comeet\.co|comeet\.com)$/i },
  { id: "polymer", host: /(^|\.)(jobs\.polymer\.co)$/i },
  { id: "recruiterflow", host: /(^|\.)(recruiterflow\.com)$/i },
];

for (const spec of COMMON_ATS) ADAPTERS.push(buildAtsAdapter(spec));
