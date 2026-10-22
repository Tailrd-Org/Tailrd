/**
 * The Store serves 0.4.0. The 0.5.0 zip (31a4371) predates the null-safe tailor card (a
 * withheld engine score shows as words, never "Match 0.0" or "ATS null") and must never be
 * uploaded; 0.5.1 carried that fix but no zip was built of it. The next Store build is
 * 0.6.0 (2026-10-22, autofill rounds 4 to 8), above every zip ever built, so an old file
 * cannot pass for it. The manifest and package.json move together.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const json = (rel: string) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8")) as { version: string };

describe("store version", () => {
  it("is 0.6.0 in the manifest and package.json", () => {
    expect(json("../manifest.json").version).toBe("0.6.0");
    expect(json("../package.json").version).toBe("0.6.0");
  });
});
