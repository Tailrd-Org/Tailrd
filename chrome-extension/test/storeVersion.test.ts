/**
 * The null-safe tailor card (a withheld engine score shows as words, never "Match 0.0" or
 * "ATS null") ships in 0.5.1. The 0.5.0 zip built before it (31a4371) must never be uploaded,
 * so the owner's next Store build has to carry a version above it (final review I-2). The
 * manifest and package.json move together.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const json = (rel: string) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8")) as { version: string };

describe("store version", () => {
  it("is 0.5.1 in the manifest and package.json", () => {
    expect(json("../manifest.json").version).toBe("0.5.1");
    expect(json("../package.json").version).toBe("0.5.1");
  });
});
