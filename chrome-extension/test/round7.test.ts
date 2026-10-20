// @vitest-environment-options {"url": "https://www.carvana.com/careers/apply/?gh_jid=8234285"}
/**
 * Round 7 (2026-10-20): each test is a read or a write a live page got wrong;
 * markup and labels verbatim from the live pages.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { scanPage } from "../src/content/formScanner";
import { readComboboxValue } from "../src/content/comboboxEngine";
import * as P from "./e2e/profiles.mjs";
import type { UserApplicationProfile } from "../src/shared/types";
import { stubLayout } from "./helpers/layout";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());
beforeEach(() => {
  document.body.innerHTML = "";
});

const persona = (who: string) => (P as Record<string, unknown>)[who] as UserApplicationProfile;

describe("Carvana's custom select reads what it shows (r4r-embed-carvana)", () => {
  // The button keeps its prompt ("Select answer") above the pick ("Yes"), each
  // in its own element, so it reads "Select answerYes". Taken whole for a
  // prompt, every pick read back empty: telemetry said "reverted,
  // value_cleared_after_write" on four questions while the page held them.
  const BEFORE = readFileSync("test/fixtures/carvana/select-before.html", "utf8");
  const AFTER = readFileSync("test/fixtures/carvana/select-after.html", "utf8");
  const button = (): HTMLElement => document.querySelector<HTMLElement>('button[aria-haspopup="listbox"]')!;

  it("reads the pick beside the prompt", () => {
    document.body.innerHTML = AFTER;
    expect(button().textContent).toBe("Select answerYes");
    expect(readComboboxValue(button())).toBe("Yes");
  });

  it("reads nothing while only the prompt shows", () => {
    document.body.innerHTML = BEFORE;
    expect(readComboboxValue(button())).toBeUndefined();
  });

  it("so the scan holds the page's answer, and an unpicked one stays blank", () => {
    document.body.innerHTML = `<form>${AFTER}</form>`;
    const picked = scanPage(persona("COMPLETE_CANADIAN"), true).fields.find((f) => f.label.startsWith("Are you 18"));
    expect(picked?.currentValue).toBe("Yes");
    document.body.innerHTML = `<form>${BEFORE}</form>`;
    const blank = scanPage(persona("COMPLETE_CANADIAN"), true).fields.find((f) => f.label.startsWith("Are you 18"));
    expect(blank?.currentValue).toBeUndefined();
    expect(blank?.proposedValue).toBe("Yes");
  });
});
