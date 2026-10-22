/**
 * Option matching, shared by the isolated content script (selects, radios,
 * comboboxes, scan-time snapping) and the MAIN-world driver (react-select,
 * Workday prompts). Pure: no DOM, no chrome.*, so it can be bundled into the
 * page-world script. One matcher for every path: the driver used to keep its
 * own looser copy (substring containment, first option wins), which put
 * "December 2026 - November 2027" on a graduation year "2027" that two options
 * fit (Greenhouse, live 2026-10-03).
 */
import { isBooleanOptionSet, optionPolarity } from "./answerKind";
import { countryFromName, regionFromText } from "./geo";

/** Lowercase words: camelCase split, punctuation and separators to spaces. */
export function normalize(text: string): string {
  return text
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2") // split camelCase
    // Accents folded, never a word break: "José" was "jos" against an option's
    // "Jose" (Ramp's school list on Ashby, live 2026-10-03).
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ") // punctuation/separators → space
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Option matching. A choice control only ever gets one of its REAL options,
 * and only when the match is confident; when it is not, the answer is "no
 * match" and the field stays blank. Tiers, strictest first:
 *
 *  1. exact value / exact visible text
 *  2. yes/no POLARITY: an answer that reads as Yes or No ("No", "I am not a
 *     protected veteran", "Not authorized") can only select an option of the
 *     same polarity, and only when exactly one exists. It never falls through
 *     to the fuzzy tiers: a "Yes" must not select "Maybe So", and a negated
 *     answer must not select the affirmative option that shares its words.
 *     Conversely a value with NO polarity ("Quebec") never selects an option
 *     of a yes/no set, whatever words they share.
 *  3. word containment ("No" → "No, I do not require sponsorship"), on whole
 *     words, never characters ("no" is not inside "not"); the most specific
 *     option wins and a tie is refused
 *  4. numeric range ("1.42" → "1-3 years"); a number on the boundary of two
 *     buckets is refused rather than placed in the first
 *  5. token overlap ("Ottawa, ON, Canada" → "Canada"); a tie is refused
 */
export function matchOption<T>(
  items: T[],
  getText: (item: T) => string,
  getValue: (item: T) => string,
  target: string
): T | null {
  const t = normalize(target);
  if (!t) return null;

  for (const item of items) if (getValue(item) === target) return item;
  for (const item of items) if (normalize(getText(item)) === t) return item;

  // Tier 2: polarity. Decided here and never revisited by a fuzzier tier.
  const texts = items.map((item) => getText(item));
  const polarities = texts.map((text) => optionPolarity(text));
  const targetPolarity = optionPolarity(target);
  if (targetPolarity !== null) {
    const same = items.filter((_, i) => polarities[i] === targetPolarity);
    if (same.length === 1) return same[0];
    if (same.length > 1) {
      // Several options share the polarity ("No", "No, but in the future"):
      // only a bare "Yes"/"No" option is the unambiguous one.
      const bare = same.filter((item) => /^(yes|no|true|false|y|n)$/.test(normalize(getText(item))));
      return bare.length === 1 ? bare[0] : null;
    }
    // No option carries this polarity. Only an option set with no yes/no
    // meaning at all may still be searched (a "No" answer to "Clearance
    // type: None | Secret | Top Secret" reads "None" as its polarity anyway).
    if (polarities.some((p) => p !== null)) return null;
  } else if (isBooleanOptionSet(texts)) {
    // A value that is not a yes/no answer cannot answer a yes/no choice.
    return null;
  }

  // An option naming alternatives with a slash is each of them exactly:
  // "Electrical/Electronics Engineering" is "Electrical Engineering", which
  // took the broad "Engineering" beside it (ServiceNow on SmartRecruiters,
  // live, round 8); "Computer Science/Software Engineering" is "Software
  // Engineering" too.
  const alternatives = items.filter((item) => slashAlternatives(getText(item)).includes(t));
  if (alternatives.length === 1) return alternatives[0];

  // Bucketed numeric options ("0-1 year", "1-3 years") are matched by range
  // only: as words, "1" is inside both "0-1 year" and "1-3 years".
  const ranged = items.filter((item) => parseRange(getText(item)) !== null);

  // Tier 3: whole-word containment, most specific option first.
  const tWords = t.split(" ");
  const contained: Array<{ item: T; size: number; first: boolean }> = [];
  for (const item of ranged.length >= 2 ? [] : items) {
    const text = normalize(getText(item));
    if (!text) continue;
    const words = text.split(" ");
    if (tWords.length === 1) {
      if (words.includes(t)) contained.push({ item, size: -words.length, first: words[0] === t });
    } else if (containsWords(words, tWords) || containsWords(tWords, words)) {
      // Bigger is better when the option is INSIDE the answer ("Software
      // Engineer" in "Software Engineer Intern"); tighter is better when the
      // answer is inside the option.
      contained.push({ item, size: containsWords(tWords, words) ? words.length : -words.length, first: false });
    }
  }
  if (contained.length > 0) {
    // A bare number (a year, a count) inside several options is not narrowed
    // by how many OTHER words each adds: "2027" fits "January - June 2027"
    // and "December 2027" equally, and the shorter one was picked (Ashby,
    // live 2026-10-03). Refuse.
    if (/^\d+$/.test(t) && contained.length > 1) return null;
    const firstWord = contained.filter((c) => c.first);
    const pool = firstWord.length > 0 ? firstWord : contained;
    const bestSize = Math.max(...pool.map((c) => c.size));
    const best = pool.filter((c) => c.size === bestSize);
    if (best.length === 1) return best[0].item;
    return null; // equally good candidates: ambiguous, refuse
  }

  // Tier 4: bucketed numeric options ("2-3 years", "$90,000-$110,000", "6+
  // years") all reduce to the same handful of tokens ("years" / "000") once
  // normalized, so generic token overlap can't tell them apart. Place the
  // answer's number inside exactly one bucket, or refuse.
  // A DATE is no number to place in buckets: "2027-05-03" read as 2027 landed
  // in "12+ weeks from offer acceptance" (Striveworks, live 2026-10-03: right
  // only by luck; a start next week would have landed there too).
  const isDate = /^\d{4}-\d{1,2}(-\d{1,2})?$|^\d{1,2}[/.-]\d{1,2}[/.-]\d{4}$/.test(target.trim());
  const targetNum = isDate ? null : firstNumber(target);
  if (targetNum !== null && ranged.length > 0) {
    const hits = ranged.filter((item) => {
      const range = parseRange(getText(item))!;
      return targetNum >= range[0] && targetNum <= range[1];
    });
    if (hits.length === 1) return hits[0];
    if (hits.length > 1) return null; // on a shared boundary: ambiguous
  }

  // A bucketed-range option set that the range tier could not place the answer
  // in must FAIL here: the buckets normalize to the same tokens ("years",
  // "000"), so token overlap would just select the first bucket, a confidently
  // wrong answer. No match lets the re-ask round supply the real options.
  if (ranged.length >= 2) return null;

  const targetTokens = t.split(" ").filter((w) => w.length > 2);
  const targetSet = new Set(targetTokens);
  let best: { item: T; score: number } | null = null;
  let tied = false;
  for (const item of items) {
    const tokens = normalize(getText(item))
      .split(" ")
      .filter((w) => w.length > 2);
    if (tokens.length === 0) continue;
    // A token overlaps on equality OR as a morphological variant ("canada" ↔
    // "canadian"): AI answers often use one. A variant shares at least 5
    // leading characters AND most of the shorter word; a 5-letter stem alone
    // made "Mechanical Engineering" a perfect match for "Mechatronics
    // Engineering" (Greenhouse discipline list, live 2026-10-03).
    const overlap = tokens.filter(
      (w) => targetSet.has(w) || targetTokens.some((tw) => isVariant(w, tw))
    ).length;
    const score = overlap / tokens.length;
    // Incidental overlap must not select: one shared generic token scores 0.5
    // on a two-token option ("University of Ottawa" → "University of
    // Oklahoma"). Require at least two shared tokens, or a fully-covered
    // single-token option ("Canadian", "Canada").
    if (score < 0.5 || (overlap < 2 && score !== 1)) continue;
    if (!best || score > best.score) {
      best = { item, score };
      tied = false;
    } else if (score === best.score) {
      tied = true;
    }
  }
  return best && !tied ? best.item : null;
}

/**
 * The readings of an option with a slash between words, normalized: each side
 * whole ("Computer Science/Software Engineering": "computer science",
 * "software engineering"), and each word of the pair with the words around
 * it ("Electrical/Electronics Engineering": "electrical engineering",
 * "electronics engineering"). None without a slash.
 */
function slashAlternatives(text: string): string[] {
  const m = /^(.*?)(\S+)\s*\/\s*(\S+)(.*)$/.exec(text.trim());
  if (!m) return [];
  const [, before, left, right, after] = m;
  // Words on both sides: a date ("05/2027") or "N/A" names no alternatives.
  if (!/\p{L}{2,}/u.test(left) || !/\p{L}{2,}/u.test(right)) return [];
  return [...new Set([`${before}${left}`, `${right}${after}`, `${before}${left}${after}`, `${before}${right}${after}`].map(normalize).filter(Boolean))];
}

/** `needle`'s words appear contiguously, in order, inside `hay`. */
function containsWords(hay: string[], needle: string[]): boolean {
  if (needle.length === 0 || needle.length > hay.length) return false;
  outer: for (let i = 0; i + needle.length <= hay.length; i++) {
    for (let j = 0; j < needle.length; j++) if (hay[i + j] !== needle[j]) continue outer;
    return true;
  }
  return false;
}

/** Length of the common leading substring of two tokens. */
function sharedPrefixLen(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a[i] === b[i]) i++;
  return i;
}

/** Two forms of one word ("canada" / "canadian" / "canadien", "engineer" /
 *  "engineering"): a 5+ letter stem with at most an ending (3 letters) past it
 *  on either side. Not two words sharing a stem ("mechanical" / "mechatronics"). */
function isVariant(a: string, b: string): boolean {
  const shared = sharedPrefixLen(a, b);
  if (shared < 5 || Math.max(a.length, b.length) - shared > 3) return false;
  // Two different places are no variants of each other: Bengaluru, India was
  // placed in "Indiana" (Starburst, Doximity; question bank 2026-10-05).
  const pa = placeKey(a);
  const pb = placeKey(b);
  return pa === null || pb === null || pa === pb;
}

/** The place a single word names (a country, a US state or Canadian province). */
function placeKey(word: string): string | null {
  const c = countryFromName(word);
  if (c) return `country:${c.code}`;
  const r = regionFromText(word);
  return r ? `region:${r.country}:${r.code}` : null;
}

/** The first number (comma thousands-separators tolerated) mentioned in text, or null. */
function firstNumber(text: string): number | null {
  const m = text.replace(/,/g, "").match(/\d+(?:\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
}

/**
 * Parse a bucketed-range option label ("2-3 years", "$90,000-$110,000",
 * "6+ years", "Under 1 year") into an inclusive [min, max] (Infinity for an
 * open end), or null when the text isn't a recognizable numeric range.
 */
function parseRange(text: string): [number, number] | null {
  // Strip thousands separators and currency symbols so "$50,000-$70,000"
  // reads as "50000-70000", a "$" between the dash and the second number
  // would otherwise break the separator match below.
  const cleaned = text.replace(/,/g, "").replace(/[$€£¥]/g, "");
  const between = cleaned.match(/(\d+(?:\.\d+)?)\s*(?:-|to|–|—)\s*(\d+(?:\.\d+)?)/i);
  if (between) return [parseFloat(between[1]), parseFloat(between[2])];
  const plus = cleaned.match(/(\d+(?:\.\d+)?)\s*\+/);
  if (plus) return [parseFloat(plus[1]), Infinity];
  // "Over 6 years" / "More than 10" exclude their bound; "6 or more" includes it.
  const over = cleaned.match(/(?:over|more than|greater than|above|>)\s*(\d+(?:\.\d+)?)/i);
  if (over) return [parseFloat(over[1]) + 1e-9, Infinity];
  const orMore = cleaned.match(/(\d+(?:\.\d+)?)\s*(?:years?\s*)?(?:or more|and (?:up|above|over))/i);
  if (orMore) return [parseFloat(orMore[1]), Infinity];
  const under = cleaned.match(/(?:under|less than|fewer than|below|<)\s*(\d+(?:\.\d+)?)/i);
  if (under) return [-Infinity, parseFloat(under[1]) - 1e-9];
  return null;
}

