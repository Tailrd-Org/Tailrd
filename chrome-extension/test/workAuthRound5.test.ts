/**
 * Decide B fix round 5 (decide-b-round5-brief.md, after decide-b-review4.md).
 * The backend port mirrors every test here in backend/tests/test_profile_facts_round5.py.
 *
 * - Veto lexicon per segment: a segment that holds a third-party, negation,
 *   past, future/conditional or pending cue gets no positive status (citizen,
 *   permanent resident, authorized, visa held) for any country. The veto never
 *   turns into a No. The cues live in backend/data/work_auth_veto.json, read
 *   by both languages.
 * - A sponsorship need stated for one country applies only to that country;
 *   "now" and "currently" start a new segment ("was X, now Y").
 * - The input is capped at 4,000 characters before parsing, and the review4
 *   pathological inputs at 100,000 characters each read in under 200 ms.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";
import { INPUT_CAP, authorizedIn, needsSponsorshipIn, polarityOf, workAuthFacts } from "../src/content/profileFacts";
import type { UserApplicationProfile } from "../src/shared/types";

const auth = (wa: string, rs = "") => workAuthFacts({ workAuthorization: wa, requiresSponsorship: rs } as UserApplicationProfile);
const VETO_FILE = join(__dirname, "..", "..", "backend", "data", "work_auth_veto.json");

describe("the shared veto lexicon", () => {
  const lexicon = JSON.parse(readFileSync(VETO_FILE, "utf8")) as Record<string, unknown>;
  it("holds every cue family of the ruling", () => {
    const RULING: Record<string, string[]> = {
      third_party: ["wife", "husband", "spouse", "partner", "fiance", "fiancee", "parent", "parents", "mother", "father", "mom", "dad", "family", "sister", "brother", "son", "daughter", "child", "children", "sponsor"],
      negation: ["not", "neither", "nor", "never", "no longer", "without", "lost", "revoked", "denied", "rejected", "ineligible"],
      past: ["was", "were", "used to", "previously", "formerly", "former", "expired", "lapsed", "abandoned", "until", "had"],
      future: ["will", "would", "once", "when", "after", "upon", "if", "planning", "plan", "hope", "expect", "going to", "soon", "future"],
      pending: ["pending", "applied", "applying", "application", "waiting", "awaiting", "in process", "processing", "lottery", "eligible", "eligibility", "need", "needs", "require", "requires", "requiring", "sponsorship request"],
    };
    for (const [family, words] of Object.entries(RULING)) {
      const listed = lexicon[family] as string[];
      for (const w of words) expect(listed, `${family}: ${w}`).toContain(w);
    }
    expect(lexicon.prefixes).toEqual(expect.arrayContaining(["non-", "ex-"]));
  });
});

/** Review4 wrong-yes rows and more of the same families: no positive status
 *  for the countries named. */
const VETOED: Array<[string, string[]]> = [
  // Third party.
  ["My husband is a US citizen and we recently married, so I am in the process of applying for a green card through him. Until that is approved I do not have permission to work in the US.", ["US"]],
  ["My wife is a US citizen", ["US"]],
  ["My father is Canadian citizen", ["CA"]],
  ["My spouse holds a green card; I am on H-4", ["US"]],
  ["My mother is a British citizen, I am applying for citizenship by descent", ["GB"]],
  ["Married to a Canadian citizen, sponsorship application in progress", ["CA"]],
  ["My partner is an American citizen. I am a Canadian citizen.", ["US"]],
  ["Both my parents are US citizens but I was born in Canada and only hold Canadian citizenship", ["US"]],
  ["My wife's employer sponsored her H-1B; I am her H-4 dependent without EAD", ["US"]],
  ["My son is a Canadian citizen", ["CA"]],
  ["Not me, my brother is the US citizen; I need sponsorship", ["US"]],
  ["My fiancee is a Canadian PR", ["CA"]],
  // Negation.
  ["Canadian citizen. Not a US citizen. Not a US PR.", ["US"]],
  ["Neither Canadian citizen nor PR", ["CA"]],
  ["Not yet a citizen", []],
  ["Used to be a US citizen but renounced it in 2019; now Canadian citizen only", ["US"]],
  ["Non-US citizen", ["US"]],
  ["US work permit revoked", ["US"]],
  ["Canadian PR status lost", ["CA"]],
  // Past.
  ["I was previously on an H-1B but my status expired when I left my job in March, and I'm now back in India. I would need a new H-1B petition to return to the US.", ["US"]],
  ["I had a green card but it was abandoned when I moved back to Canada; Canadian citizen", ["US"]],
  ["Formerly a US permanent resident", ["US"]],
  ["Ex-US citizen", ["US"]],
  ["Expired Canadian work permit", ["CA"]],
  ["Was a Canadian PR", ["CA"]],
  // Future, conditional, pending, needed.
  ["As a Canadian citizen I can work in Canada freely, and under USMCA I would be able to obtain TN status at the border for a qualifying US position, though I do not currently hold it.", ["US"]],
  ["Honestly, I'm not sure how to answer this. I'm a Canadian citizen but I've been working remotely for a US company as a contractor. I've never had a US work visa.", ["US"]],
  ["For work in USA I am requiring the sponsor of H1B", ["US"]],
  ["I would be authorized to work in the US once my green card is approved", ["US"]],
  ["Should I receive the H-1B in the lottery, I will need sponsorship", ["US"]],
  ["I will become a permanent resident when my application is finalized in the spring", []],
  ["Once I graduate in May I will be on OPT", ["US"]],
  ["Will be a US citizen next year", ["US"]],
  ["US citizenship application pending", ["US"]],
  ["Eligible to work in the US after my EAD arrives", ["US"]],
  ["Hope to get Canadian PR soon", ["CA"]],
];

describe("veto: a segment with a cue gets no positive status", () => {
  for (const [text, codes] of VETOED) {
    it(JSON.stringify(text), () => {
      const a = auth(text);
      for (const code of codes) {
        expect(authorizedIn(a, code, null)?.value ?? null, `${code} authorized`).not.toBe(true);
        expect(a.byCountry.get(code)?.basis ?? null, `${code} basis`).not.toBe("citizen");
        expect(a.byCountry.get(code)?.basis ?? null, `${code} basis`).not.toBe("permanent_resident");
      }
      if (codes.length === 0) {
        expect(authorizedIn(a, null, null)?.value ?? null).not.toBe(true);
        expect(authorizedIn(a, "CA", "CA")?.value ?? null).not.toBe(true);
      }
    });
  }
});

describe("veto: never a No, and clean segments keep their status", () => {
  it("someone else's citizenship is unknown, not a No", () => {
    const a = auth("My wife is a US citizen");
    expect(authorizedIn(a, "US", null)).toBeNull();
    expect(needsSponsorshipIn(a, "US", null)).toBeNull();
  });
  it("an explicit No is still a No", () => {
    expect(authorizedIn(auth("Canada: yes\nUS: No"), "US", null)?.value).toBe(false);
    expect(authorizedIn(auth("Not authorized to work in the US"), "US", null)?.value).toBe(false);
  });
  it("the applicant's own clause keeps its status", () => {
    // One answer, so the partner's words leave Canada unknown too (the
    // allow-list layer, D-61 amendment); never a No.
    const a = auth("My partner is an American citizen. I am a Canadian citizen.");
    expect(a.byCountry.get("CA")?.authorized ?? null).toBeNull();
    expect(auth("Canadian citizen").byCountry.get("CA")).toMatchObject({ authorized: true, needsSponsorship: false, basis: "citizen" });
    expect(auth("US permanent resident").byCountry.get("US")).toMatchObject({ authorized: true, basis: "permanent_resident" });
  });
  it("a negated sponsorship need is a sponsorship fact, not a veto on the status beside it", () => {
    for (const text of ["I am a Canadian citizen and do not require sponsorship", "Canadian citizen, no sponsorship needed", "US citizen without sponsorship"]) {
      const code = /canadian/i.test(text) ? "CA" : "US";
      expect(auth(text).byCountry.get(code), text).toMatchObject({ authorized: true, basis: "citizen" });
    }
  });
  it("'was X, now Y' is unknown under the allow-list layer (\"was\", \"now\" are outside the vocabulary)", () => {
    const a = auth("Was on a study permit, now a Canadian PR");
    expect(a.byCountry.get("CA")?.authorized ?? null).toBeNull();
    const b = auth("I was a US green card holder now I am a Canadian citizen");
    expect(b.byCountry.get("CA")?.authorized ?? null).toBeNull();
    expect(b.byCountry.get("US")?.basis ?? null).not.toBe("permanent_resident");
  });
  it("'not currently authorized' is never split into a Yes", () => {
    expect(authorizedIn(auth("Not currently authorized to work in the US"), "US", null)?.value).toBe(false);
    expect(authorizedIn(auth("I don't currently have US work authorization"), "US", null)?.value).toBe(false);
  });
});

describe("sponsorship scope: a need stated for one country stays there", () => {
  const FIVE =
    "I hold dual citizenship with the United Kingdom and Canada. I grew up in London and moved to Calgary for university, so I can work in either country without restrictions. I am not authorized to work in the United States and would need a visa.";
  it("review4 #5", () => {
    const a = auth(FIVE);
    expect(needsSponsorshipIn(a, "CA", null)?.value ?? null).not.toBe(true);
    expect(needsSponsorshipIn(a, "GB", null)?.value ?? null).not.toBe(true);
    expect(authorizedIn(a, "US", null)?.value).toBe(false);
    expect(needsSponsorshipIn(a, "US", null)?.value).toBe(true);
  });
  it("a need in a sentence that names no country still applies to the countries named", () => {
    const a = auth("I live in the US on OPT. I will need sponsorship.");
    expect(needsSponsorshipIn(a, "US", null)?.value).toBe(true);
  });
});

describe("input cap", () => {
  it("is 4,000 characters", () => {
    expect(INPUT_CAP).toBe(4000);
  });
  it("text after the cap is not read", () => {
    // Padded with a word of the allow-list vocabulary, so only the cap decides.
    const pad = "a ".repeat(INPUT_CAP / 2);
    expect(auth(`${pad} Canadian citizen`).byCountry.has("CA")).toBe(false);
    expect(auth(`Canadian citizen ${pad}`).byCountry.get("CA")?.basis).toBe("citizen");
    expect(polarityOf(`${pad} yes`)).toBeNull();
  });
  it("counts characters, not UTF-16 units (the backend counts code points)", () => {
    const astral = "\u{1F600}".repeat(INPUT_CAP - 20);
    expect(auth(`${astral} Canadian citizen`).byCountry.get("CA")?.basis).toBe("citizen");
  });
});

// --- timing: the review4 pathological inputs, truncated to 100,000 characters ----------------
/** A fixed linear congruential generator, so both languages build the same inputs. */
function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1103515245) + 12345) >>> 0;
    return s >>> 8;
  };
}
const C = ["Canada", "US", "UK", "India", "Mexico", "Germany", "the United States", "U.S.", "USA", "America"];
const rep = (s: string, n: number): string => s.repeat(n);
const cycle = <T>(xs: T[], n: number): T[] => Array.from({ length: n }, (_, i) => xs[i % xs.length]);
const pick = (xs: string[], n: number, seed: number): string[] => {
  const r = lcg(seed);
  return Array.from({ length: n }, () => xs[r() % xs.length]);
};
const PATHOLOGICAL: Record<string, string> = {
  nested_parens_50k: rep("(", 25000) + "Canadian citizen" + rep(")", 25000),
  nested_parens_country: cycle(C, 25000).map((c) => `(${c} `).join("") + rep(")", 25000),
  US_space_100k: rep("US ", 33334),
  US_nospace_100k: rep("US", 50000),
  US_comma_100k: rep("US, ", 25000),
  US_dots_100k: rep("U.S. ", 20000),
  not_country_alt: cycle(C, 12000).map((c) => `not ${c}`).join(" "),
  not_citizen_alt: cycle(C, 8000).map((c) => `not ${c} citizen`).join(" "),
  not_not_country: rep("not ", 20000) + "Canada " + rep("not ", 20000),
  country_slash: cycle(C, 50000).join("/"),
  country_and: cycle(C, 40000).join(" and "),
  colon_lines: cycle(C, 10000).map((c) => `${c}: citizen`).join("\n"),
  colon_need: cycle(C, 8000).map((c) => `${c}: needs sponsorship`).join("\n"),
  dash_pending: "green card " + rep("- ", 50000) + "pending",
  paren_pending: "green card " + rep("( ", 50000) + "pending",
  hyphen_chain: rep("TN-", 33000) + "eligible",
  H1B_variants: pick(["H-1B", "H1B", "H 1 B", "h1-b", "OPT", "STEM OPT", "EAD", "TN", "L-1", "PR", "GC"], 25000, 4).join(" "),
  mixed_punct_100k: pick(".,;:!?-()[]{}/\\|'\"_*&^%$#@~` \n\t".split(""), 100000, 5).join(""),
  spaces_100k: rep(" ", 100000),
  tabs_nl_100k: "Canadian" + rep(" \t\n\r", 25000) + "citizen",
  unicode_spaces: "US" + rep("  　", 33000) + "citizen",
  realistic_50k: rep(
    "I am a Canadian citizen living in Toronto, and I was on OPT in the U.S. previously, now I need H-1B sponsorship to work in the United States; not authorized in the UK. ",
    300
  ),
  citizen_of_list: "citizen of " + cycle(C, 50000).join(", "),
  permanent_resident_of: rep("permanent resident of ", 5000),
  need_spons_for: rep("need sponsorship for ", 5000) + "Canada",
  emdash_100k: "Canadian citizen " + rep(String.fromCharCode(0x2014) + " ", 50000) + "US",
  yes_no_alt: cycle(["yes", "no", "not", "Canada", "US"], 25000).join(" "),
};

describe("timing: the review4 pathological inputs read in under 200 ms", () => {
  beforeAll(() => {
    auth("Canadian citizen; US: no", "No");
  });
  for (const [name, full] of Object.entries(PATHOLOGICAL)) {
    const text = full.slice(0, 100_000);
    it(`${name} (${text.length} chars)`, () => {
      const t0 = performance.now();
      const a = auth(text, text);
      authorizedIn(a, "US", null);
      needsSponsorshipIn(a, "CA", null);
      polarityOf(text);
      expect(performance.now() - t0).toBeLessThan(200);
    });
  }
});
