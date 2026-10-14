/**
 * Decide B: the allow-list safety layer (decide-b-allowlist-brief.md, D-61
 * amendment "positives only from a fully recognised answer").
 * The backend port mirrors every test here in backend/tests/test_profile_facts_allowlist.py.
 *
 * - A positive status (citizen, permanent resident, authorized, visa held) is
 *   emitted only when every token of the answer is in the closed vocabulary of
 *   backend/data/work_auth_allowlist.json (plus the country names of the geo
 *   tables). One token outside it and no country gets a positive status.
 *   Negative and sponsorship facts are read as before.
 * - The answer is NFKC-normalised first; any non-ASCII letter left after that
 *   (a Cyrillic or Greek lookalike, an accent) is outside the vocabulary.
 * - A labelled multi-line answer ("Canada: citizen\nUS: no") is checked line
 *   by line.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { authorizedIn, needsSponsorshipIn, outOfVocabulary, workAuthFacts } from "../src/content/profileFacts";
import type { UserApplicationProfile } from "../src/shared/types";

const auth = (wa: string, rs = "") => workAuthFacts({ workAuthorization: wa, requiresSponsorship: rs } as UserApplicationProfile);
const basis = (wa: string, code: string) => auth(wa).byCountry.get(code)?.basis ?? null;
const authorized = (wa: string, code: string | null) => authorizedIn(auth(wa), code, null)?.value ?? null;
const ALLOW_FILE = join(__dirname, "..", "..", "backend", "data", "work_auth_allowlist.json");

describe("the shared allow-list vocabulary", () => {
  const vocab = JSON.parse(readFileSync(ALLOW_FILE, "utf8")) as Record<string, unknown>;
  it("holds the ruling's words and nothing for third parties, negation, the past or the future", () => {
    const all = Object.entries(vocab)
      .filter(([k, v]) => !k.startsWith("_") && Array.isArray(v))
      .flatMap(([, v]) => v as string[]);
    for (const w of ["yes", "no", "i", "i'm", "am", "a", "an", "the", "my", "have", "hold", "with", "citizen", "citizenship", "national", "permanent resident", "pr", "green card", "lpr", "authorized", "authorised", "eligible to work", "legally", "work authorization", "work permit", "open work permit", "pgwp", "study permit", "opt", "stem opt", "cpt", "h-1b", "h1b", "tn", "l-1", "e-3", "o-1", "visa", "status", "and", "plus", "both", "in", "of", "for", "to", "work", "working", "no sponsorship needed", "without sponsorship", "do not require sponsorship", "don't need sponsorship"]) {
      expect(all, w).toContain(w);
    }
    for (const w of ["wife", "husband", "hubby", "not", "never", "was", "former", "ex", "will", "pending", "soon", "eligible", "need"]) {
      expect(all, w).not.toContain(w);
    }
  });
});

describe("common real answers keep their positives", () => {
  it("Yes", () => expect(authorized("Yes", null)).toBe(true));
  it("Canadian citizen", () => expect(basis("Canadian citizen", "CA")).toBe("citizen"));
  it("US citizen", () => expect(basis("US citizen", "US")).toBe("citizen"));
  it("PR", () => expect(authorized("PR", null)).toBe(true));
  it("Canadian PR", () => expect(basis("Canadian PR", "CA")).toBe("permanent_resident"));
  it("Green card holder", () => expect(basis("Green card holder", "US")).toBe("permanent_resident"));
  it("Authorized to work in Canada and the US", () => {
    expect(authorized("Authorized to work in Canada and the US", "CA")).toBe(true);
    expect(authorized("Authorized to work in Canada and the US", "US")).toBe(true);
  });
  it("Yes, Canadian citizen, no sponsorship needed", () => {
    const a = auth("Yes, Canadian citizen, no sponsorship needed");
    expect(a.byCountry.get("CA")?.basis).toBe("citizen");
    expect(needsSponsorshipIn(a, "CA", null)?.value).toBe(false);
  });
  it("I am a US citizen", () => expect(basis("I am a US citizen", "US")).toBe("citizen"));
  it("Citizen of Canada", () => expect(basis("Citizen of Canada", "CA")).toBe("citizen"));
  it("Canada: Yes / US: No on two lines", () => {
    expect(authorized("Canada: Yes\nUS: No", "CA")).toBe(true);
    expect(authorized("Canada: Yes\nUS: No", "US")).toBe(false);
  });
  it("H-1B", () => {
    const a = auth("H-1B");
    expect(authorizedIn(a, "US", null)?.value).toBe(true);
    expect(needsSponsorshipIn(a, "US", null)?.value).toBe(true);
  });
  it("On OPT", () => expect(authorized("On OPT", "US")).toBe(true));
  it("PGWP", () => expect(authorized("PGWP", "CA")).toBe(true));
  it("curly apostrophes and NFKC spaces are not outside the vocabulary", () => {
    expect(basis("I’m a Canadian citizen", "CA")).toBe("citizen");
  });
});

describe("one unknown token removes every positive", () => {
  it.each([
    ["My better half is a US citizen.", "US"],
    ["Hubby is a Canadian citizen.", "CA"],
    ["US citizen. NOT.", "US"],
    ["Canadian PR (lapsed in 2022).", "CA"],
    ["Green card in the works.", "US"],
    ["Canadian citizen-to-be.", "CA"],
    ["Wed to a US citizen.", "US"],
    ["Canadian citizen, US citizen except me", "CA"],
  ])("%s", (wa, code) => {
    expect(authorized(wa, code)).not.toBe(true);
    expect(["citizen", "permanent_resident"]).not.toContain(basis(wa, code));
  });
  it.each([
    ["US citizen? No.", "US"],
    ["Am I a US citizen? No", "US"],
    ["Canadian PR: false", "CA"],
    ["green card? no", "US"],
    ["Canadian citizen n", "CA"],
    ["Working on my green card", "US"],
  ])("a negative answer word beside a status, or 'on' before no visa: %s", (wa, code) => {
    expect(authorized(wa, code)).not.toBe(true);
    expect(["citizen", "permanent_resident"]).not.toContain(basis(wa, code));
  });
  it("'on' counts before a visa, 'a' or 'an' aside", () => {
    expect(outOfVocabulary("Working on OPT")).toBe(false);
    expect(outOfVocabulary("On a work permit")).toBe(false);
    expect(outOfVocabulary("Working on PR")).toBe(true);
    expect(authorized("citizen? no", null)).not.toBe(true);
  });
  it("a bare Yes beside an unknown token is unknown, never a No", () => {
    expect(authorized("Yes, hubby", null)).toBe(null);
  });
  it("negative facts are still read", () => {
    expect(authorized("Canadian citizen, not authorized in the US", "US")).toBe(false);
    expect(authorized("Canadian citizen, not authorized in the US", "CA")).toBe(null);
  });
  it("'my' is in the vocabulary only before a status noun about the applicant", () => {
    expect(basis("My Canadian citizenship", "CA")).toBe("citizen");
    expect(basis("Canadian citizen, my", "CA")).toBe(null);
    expect(outOfVocabulary("my green card")).toBe(false);
    expect(outOfVocabulary("my PR card")).toBe(false);
    expect(outOfVocabulary("US citizen, my")).toBe(true);
  });
});

describe("normalisation", () => {
  it.each([
    ["fullwidth negation", "Ｎｏｔ a Canadian PR.", "CA"],
    ["fullwidth third party", "My ｈｕｓｂａｎｄ is a US citizen.", "US"],
    ["Cyrillic o in not", "I am nоt a US citizen.", "US"],
    ["Cyrillic a in husband", "My husbаnd is a US citizen.", "US"],
    ["accented letter", "My wifé is a Canadian citizen.", "CA"],
    ["Cyrillic i in citizen", "Canadian citіzen", "CA"],
    ["Chinese words", "我的丈夫是 US citizen.", "US"],
    ["combining accent", "US citizén", "US"],
    ["zero-width space", "US citi​zen", "US"],
  ])("%s", (_name, wa, code) => {
    expect(authorized(wa, code)).not.toBe(true);
    expect(["citizen", "permanent_resident"]).not.toContain(basis(wa, code));
  });
  it("NFKC folds fullwidth letters that are in the vocabulary", () => {
    expect(outOfVocabulary("ＵＳ citizen")).toBe(false);
  });
});

describe("labelled multi-line answers are checked line by line", () => {
  it("an unknown token on one labelled line leaves the other line's positive", () => {
    const wa = "Canada: citizen\nUS: citizen through my wife";
    expect(basis(wa, "CA")).toBe("citizen");
    expect(authorized(wa, "US")).not.toBe(true);
    expect(basis(wa, "US")).not.toBe("citizen");
  });
  it("a dash labels a line too", () => {
    expect(authorized("Canada - Yes\nUS - No", "CA")).toBe(true);
    expect(authorized("Canada - Yes\nUS - No", "US")).toBe(false);
  });
  it("lines without labels are one answer", () => {
    expect(basis("Canadian citizen\nMy wife is a US citizen", "CA")).toBe(null);
    expect(basis("Canadian citizen\nMy wife is a US citizen", "US")).toBe(null);
  });
});
