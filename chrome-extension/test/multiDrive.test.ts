/**
 * Greenhouse's remix react-select multi-select (Gallup's language list, live,
 * round 7) opens for no scripted event, so the combobox engine can add no
 * chip; the page-world driver picks each item from the widget's own options.
 * An item already shown as a chip is never picked again: react-select's
 * selectOption toggles a selected option off.
 */
import { describe, expect, it } from "vitest";
import { driveEachItem } from "../src/content/multiDrive";
import type { DriverResult } from "../src/content/mainWorldClient";

describe("driveEachItem", () => {
  it("drives each item once and reports what was added", async () => {
    const seen: string[] = [];
    const drive = async (item: string): Promise<DriverResult> => {
      seen.push(item);
      return item === "Tamil" ? { ok: false, reason: "no-match", options: ["Tamil (Ceylon)", "Tamil (India)"] } : { ok: true, committed: item };
    };
    const r = await driveEachItem("Tamil, Kannada, Hindi, Kannada", drive, () => []);
    expect(seen).toEqual(["Tamil", "Kannada", "Hindi"]);
    expect(r.added).toEqual(["Kannada", "Hindi"]);
    expect(r.options).toEqual(["Tamil (Ceylon)", "Tamil (India)"]);
  });

  it("never picks an item the widget already shows (a second pick would remove it)", async () => {
    const seen: string[] = [];
    const drive = async (item: string): Promise<DriverResult> => {
      seen.push(item);
      return { ok: true, committed: item };
    };
    const r = await driveEachItem("Korean, English", drive, () => ["Korean"]);
    expect(seen).toEqual(["English"]);
    expect(r.added).toEqual(["Korean", "English"]);
  });
});
