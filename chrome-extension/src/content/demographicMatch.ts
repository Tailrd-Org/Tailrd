/**
 * On-device closest-option matcher for EEO / demographic dropdowns. Given the
 * user's profile value and the widget's REAL options, returns the nearest
 * available option, so "Arab" fills a US-Census race dropdown as "White" or a
 * MENA option when offered. Never sent to any server; demographic answers stay
 * on the device by policy.
 */
import type { FieldCategory } from "../shared/types";

const DECLINE_PATTERNS = [
  "prefer not",
  "decline",
  "do not wish",
  "wish to answer", // catches "I don't wish to answer" (apostrophe → space on normalize)
  "not to disclose",
  "not disclosed",
  "choose not",
  "rather not",
  // "I do not want to answer" (the federal disability form, CC-305), "I don't
  // want to answer": a stated "Prefer not to say" found no decline among them.
  "not want to",
  "don t want to",
];

const norm = (s: string): string => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

/** `needle` appears in `hay` as whole words. Substrings read "male" inside
 *  "female" and "man" inside "woman" (2026-10-03). */
const hasWords = (hay: string, needle: string): boolean => ` ${hay} `.includes(` ${needle} `);

/** Several options each holding the whole answer: each NARROWER than it
 *  ("Asian" in East, South and Southeast Asian). No pick, by any matcher. */
export function severalHold(value: string, options: string[]): boolean {
  const v = norm(value);
  return Boolean(v) && options.filter((o) => hasWords(norm(o), v)).length > 1;
}

/** Priority-ordered option substrings for a normalized profile value. */
const RACE: Record<string, string[]> = {
  "arab": ["middle eastern", "north african", "mena", "white"],
  "middle eastern": ["middle eastern", "north african", "mena", "white"],
  "north african": ["north african", "middle eastern", "mena", "white"],
  "persian": ["middle eastern", "mena", "white", "asian"],
  "hispanic": ["hispanic", "latino", "latinx"],
  "latino": ["hispanic", "latino", "latinx"],
  "south asian": ["asian", "south asian"],
  "east asian": ["asian", "east asian"],
  "desi": ["asian", "south asian"],
  "caucasian": ["white", "caucasian"],
  // "Caucasian" alone in the list (AltaML on Lever, question bank 2026-10-08).
  "white": ["white", "caucasian", "european"],
  "black": ["black", "african american", "african"],
  "african american": ["black", "african american", "african"],
  // The US categories' own names, which other lists word differently
  // ("Black / Of African descent", Chime; question bank 2026-10-05).
  "black or african american": ["black", "african american", "african"],
  "hispanic or latino": ["hispanic", "latino", "latinx"],
  "american indian or alaska native": ["american indian", "alaska native", "alaskan native", "native american", "indigenous"],
  "native hawaiian or other pacific islander": ["native hawaiian", "pacific islander"],
  "two or more races": ["two or more", "multiracial", "mixed"],
  "indigenous": ["native", "indigenous", "aboriginal", "first nations"],
  "native american": ["native", "indigenous", "american indian"],
  "mixed": ["two or more", "multiracial", "mixed"],
  "biracial": ["two or more", "multiracial", "mixed"],
};

const GENDER: Record<string, string[]> = {
  "man": ["male", "man"],
  "male": ["male", "man"],
  "woman": ["female", "woman"],
  "female": ["female", "woman"],
  "non binary": ["non binary", "nonbinary", "genderqueer"],
};

const VETERAN: Record<string, string[]> = {
  "no": ["not a protected veteran", "not a veteran", "no"],
  "not a veteran": ["not a protected veteran", "not a veteran", "no"],
  "yes": ["identify as one or more", "protected veteran", "yes"],
  "protected veteran": ["identify as one or more", "protected veteran", "yes"],
  "veteran": ["protected veteran", "veteran", "yes"],
};

const DISABILITY: Record<string, string[]> = {
  "no": ["no i do not", "do not have", "no"],
  "yes": ["yes i have", "have a disability", "yes"],
};

const ORIENTATION: Record<string, string[]> = {
  "straight": ["heterosexual", "straight"],
  "heterosexual": ["heterosexual", "straight"],
  "gay": ["gay", "lesbian", "homosexual"],
  "lesbian": ["lesbian", "gay", "homosexual"],
  "homosexual": ["gay", "lesbian", "homosexual"],
  "bisexual": ["bisexual", "bi"],
  "queer": ["queer", "lgbtq"],
  "asexual": ["asexual"],
  "pansexual": ["pansexual", "bisexual"],
};

function tableFor(category: FieldCategory): Record<string, string[]> | null {
  switch (category) {
    case "eeoRace":
    case "eeoHispanic":
      return RACE;
    case "eeoGender":
    case "eeoGenderIdentity":
      return GENDER;
    case "eeoVeteran":
      return VETERAN;
    case "eeoDisability":
      return DISABILITY;
    case "eeoSexualOrientation":
      return ORIENTATION;
    default:
      return null;
  }
}

/** What a veteran-status answer (the profile's, or an option) claims. */
type VeteranClaim =
  | "decline"
  | "protected" // a protected veteran
  | "not-protected" // not a protected veteran: may or may not have served
  | "never-served" // not a veteran at all
  | "veteran" // served (protection unstated)
  | "veteran-unprotected" // served, not protected
  | "serving" // active duty, reserve, national guard
  | null;

function veteranClaim(text: string): VeteranClaim {
  const t = norm(text);
  if (!t) return null;
  if (DECLINE_PATTERNS.some((d) => t.includes(d)) || /\bdecline\b/.test(t)) return "decline";
  if (/\b(non|un) ?protected veterans?\b/.test(t)) return "veteran-unprotected";
  // "I am a veteran but not a protected veteran" served (Veolia,
  // SmartRecruiters bank, round 7): read as "not a protected veteran", it
  // tied with that option, and a never-served applicant got neither.
  if (/\bveterans?\b[^.]{0,20}\bbut\b[^.]{0,10}\bnot (a )?protected\b/.test(t)) return "veteran-unprotected";
  // Served, outside the categories listed: "I previously served in the
  // military (US), but none of the above apply to me." (Insomniac on
  // Greenhouse, live, round 8: read as a veteran, it tied with the
  // protected veteran's option).
  if (/\bserved\b[^.]{0,60}\bbut\b[^.]{0,20}\bnone of the above\b/.test(t)) return "veteran-unprotected";
  if (/\bnot (a )?protected veterans?\b|\bnot protected\b/.test(t)) return "not-protected";
  // A protected veteran negated further back: "I do not fall into one of the
  // above categories of 'protected veteran'" (Insomniac) read as protected,
  // and someone who never served got no option at all.
  if (/\b(do not|don t|does not|am not|is not)\b[^.]{0,60}\bprotected veterans?\b/.test(t)) return "not-protected";
  if (/\bprotected veterans?\b/.test(t)) return "protected";
  // "I am not a U.S. veteran" (Gopuff on Lever, question bank 2026-10-08).
  if (/\bnever served\b|\bno military (service|experience)\b|\bnot (a |an )?(u s |us |american )?veterans?\b|\bnon ?veterans?\b/.test(t)) return "never-served";
  if (/\bactive duty\b|\bnational guard\b|\breserves?\b|\bcurrently serving\b/.test(t)) return "serving";
  if (/\bveterans?\b|\bserved\b/.test(t) && !/\b(not|no|never)\b/.test(t)) return "veteran";
  return null;
}

/**
 * The veteran-status option the profile's answer settles, or null when it
 * settles none (left for the user), or undefined when the options are not a
 * veteran vocabulary at all. "I am not a protected veteran" says nothing
 * about having served: it fills only an option saying exactly that, never
 * "I have never served" or "I am not a veteran" (decided with the user,
 * 2026-10-03, on Robinhood's seven military statuses). A protected veteran is
 * a veteran, but which protected category (ActioNet lists four) is theirs.
 */
export function veteranOption(value: string, label: string, options: string[]): string | null | undefined {
  const claim = veteranClaim(value);
  if (!claim) return undefined;
  const real = options.filter((o) => o.trim() && !/^(select|choose)\b/i.test(o.trim()));
  const claims = real.map((o) => ({ raw: o, claim: veteranClaim(o) }));
  const decline = claims.filter((c) => c.claim === "decline");
  if (claim === "decline") return decline.length === 1 ? decline[0].raw : null;
  // A yes/no question: the label says what Yes means.
  const yes = real.filter((o) => /^(yes|y)\b/i.test(o.trim()));
  const no = real.filter((o) => /^(no|n)\b/i.test(o.trim()));
  if (yes.length === 1 && no.length === 1 && claims.every((c) => c.claim === null || c.claim === "decline" || c.raw === yes[0] || c.raw === no[0])) {
    const asksProtected = /\bprotected\b/i.test(label);
    if (claim === "protected") return yes[0];
    if (claim === "never-served") return no[0];
    if (claim === "not-protected") return asksProtected ? no[0] : null;
    return null;
  }
  if (claims.every((c) => c.claim === null)) return undefined;
  const pick = (accepted: VeteranClaim[]): string | null => {
    const hits = claims.filter((c) => accepted.includes(c.claim));
    return hits.length === 1 ? hits[0].raw : null;
  };
  switch (claim) {
    case "protected":
      return pick(["protected", "veteran"]);
    case "never-served":
      return pick(["never-served"]) ?? pick(["not-protected"]);
    case "not-protected":
      return pick(["not-protected"]);
    default:
      return null;
  }
}

/**
 * A race list that also asks Hispanic origin (EEO-1's combined form: "White
 * (not Hispanic or Latino)" … "Hispanic or Latino"). A Hispanic applicant is
 * the Hispanic option whatever their race; "Two or more races (not Hispanic or
 * Latino)" contradicted a stated "Yes" (Vagaro on Breezy, live 2026-10-03).
 * undefined: not a combined list, or the applicant is not stated Hispanic
 * (race matching as usual); null: no single Hispanic option to take.
 */
export function hispanicRaceOption(hispanic: string, options: string[]): string | null | undefined {
  if (!options.some((o) => /\bnot hispanic\b/i.test(o))) return undefined;
  if (!/^(yes|y|hispanic|latin[oax])\b/i.test(hispanic.trim())) return undefined;
  const hits = options.filter((o) => /\b(hispanic|latin[oax])/i.test(o) && !/\bnot hispanic\b/i.test(o));
  return hits.length === 1 ? hits[0] : null;
}

/** The group a stated race belongs to on a grouped list, by its words. */
const RACE_GROUPS: Array<[RegExp, string]> = [
  [/\b(white|caucasian|european)\b/, "white"],
  [/\b(black|african)\b/, "black"],
  [/\b(asian|chinese|indian|pakistani|bangladeshi|korean|japanese|vietnamese|filipino)\b/, "asian"],
  [/\b(two or more|mixed|multiracial|biracial)\b/, "mixed"],
];
/** A race named only by its group, as US forms record it: no subgroup in it
 *  ("African" inside "Black or African American" is the group's name). */
const GROUP_ONLY = /^(white|caucasian|black|african american|black or african american|asian|two or more races|mixed|multiracial|biracial)$/;

/**
 * An ethnicity list grouped "Group: subgroup" (the UK census: "White: Irish",
 * "White: Any other White background", "Asian/Asian British: Chinese"). A
 * race stated by its group alone takes the group's "any other" option; a
 * subgroup is chosen only when the stated race names it ("White Irish").
 * "White: Irish" was chosen for a German (Spotify on Lever, question bank
 * 2026-10-08). undefined: not such a list, or no group of it is the
 * applicant's; null: no single option to take.
 */
export function groupedEthnicityOption(value: string, options: string[]): string | null | undefined {
  const grouped = options
    .map((raw) => ({ raw, m: /^([^:]{2,40}):\s*(.+)$/.exec(raw.trim()) }))
    .filter((x): x is { raw: string; m: RegExpExecArray } => x.m !== null);
  if (grouped.length < 4) return undefined;
  const v = norm(value);
  const group = RACE_GROUPS.find(([re]) => re.test(v))?.[1];
  if (!group) return undefined;
  const mine = grouped.filter((x) => hasWords(norm(x.m[1]), group));
  if (mine.length < 2) return undefined;
  if (!GROUP_ONLY.test(v)) {
    const sub = mine.filter((x) => !/\bany other\b/.test(norm(x.m[2])) && hasWords(v, norm(x.m[2])));
    if (sub.length === 1) return sub[0].raw;
  }
  const other = mine.filter((x) => /\bany other\b/.test(norm(x.m[2])));
  return other.length === 1 ? other[0].raw : null;
}

/** An option that declines to answer ("Decline to answer", "Prefer not to say"). */
export function isDeclineText(text: string): boolean {
  const t = norm(text);
  return Boolean(t) && (DECLINE_PATTERNS.some((d) => t.includes(d)) || /^decline\b/.test(t));
}

/** The option that declines to answer ("I don't wish to answer", "Decline To
 *  Self Identify"), when the list has exactly one. */
export function declineOption(options: string[]): string | null {
  const hits = options.filter((o) => DECLINE_PATTERNS.some((d) => norm(o).includes(d)));
  return hits.length === 1 ? hits[0] : null;
}

export function closestDemographicOption(
  category: FieldCategory,
  value: string,
  options: string[]
): string | null {
  const opts = options.map((o) => ({ raw: o, n: norm(o) })).filter((o) => o.n.length > 0);
  const v = norm(value);
  if (opts.length === 0 || !v) return null;

  // 1. The answer itself, then the one option holding it ("Asian" in "Asian
  //    (Not Hispanic or Latino)"), then the longest option it holds ("No" in
  //    "No, I do not have a disability"). Several options holding it are each
  //    NARROWER than the answer: "Asian" names none of East, South and
  //    Southeast Asian, and the first listed won (a real profile on Robinhood,
  //    2026-10-03). Ambiguity is no answer, and no decline either: the user
  //    gave one, so the field is left for them to pick.
  const exact = opts.find((o) => o.n === v);
  if (exact) return exact.raw;
  const holding = opts.filter((o) => hasWords(o.n, v));
  if (holding.length > 0) return holding.length === 1 ? holding[0].raw : null;
  const inside = opts.filter((o) => hasWords(v, o.n));
  // "Gay or Lesbian" holds both "Gay" and "Lesbian": an either-or names
  // neither (Superhuman ticked "Lesbian", round 3, 2026-10-03).
  if (inside.length > 1 && /\bor\b/.test(v)) return null;
  if (inside.length > 0) {
    const longest = Math.max(...inside.map((o) => o.n.length));
    const best = inside.filter((o) => o.n.length === longest);
    return best.length === 1 ? best[0].raw : null;
  }

  // 2. Synonym / nearest-neighbour candidates, in priority order: the option
  //    that IS the candidate, else the one option holding it. "man" is held by
  //    "Cisgender man" and "Transgender man" alike: no answer.
  for (const cand of tableFor(category)?.[v] ?? []) {
    const same = opts.find((o) => o.n === cand);
    if (same) return same.raw;
    const hits = opts.filter((o) => hasWords(o.n, cand));
    if (hits.length > 0) return hits.length === 1 ? hits[0].raw : null;
  }

  // 3. A stated DECLINE finds the list's decline option. A substantive answer
  //    the list cannot hold ("Two or More Races" on a mark-all-that-apply list)
  //    is left to the applicant, never declined on their behalf.
  if (!isDeclineText(value)) return null;
  const decline = opts.find((o) => DECLINE_PATTERNS.some((d) => o.n.includes(d)));
  return decline ? decline.raw : null;
}
