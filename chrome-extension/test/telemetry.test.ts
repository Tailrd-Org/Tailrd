import { describe, it, expect } from "vitest";
import { buildAutofillTelemetry, revertedFields } from "../src/content/telemetry";
import type { DetectedField } from "../src/shared/types";
import type { FieldReport } from "../src/content/reconciler";

const f = (id: string, label: string, category = "unknown"): DetectedField =>
  ({ id, label, category } as unknown as DetectedField);

const rep = (fieldId: string, ok: boolean, reason?: string): FieldReport => ({
  fieldId,
  ok,
  status: ok ? "stable" : "drifted",
  reason,
  attempts: 1,
});

describe("buildAutofillTelemetry", () => {
  it("summarizes filled vs failed with labels + reasons", () => {
    const fields = [f("a", "First Name", "firstName"), f("b", "Work Authorization")];
    const reports = [rep("a", true), rep("b", false, "No option matches")];

    const t = buildAutofillTelemetry(
      fields,
      { host: "boards.greenhouse.io", url: "https://x/y", atsType: "greenhouse" },
      { reports, outcomes: [] }
    );

    expect(t.totalFields).toBe(2);
    expect(t.filled).toBe(1);
    expect(t.failed).toBe(1);
    expect(t.failedFields).toEqual([
      { label: "Work Authorization", category: "unknown", reason: "No option matches" },
    ]);
    expect(t.host).toBe("boards.greenhouse.io");
    expect(t.atsType).toBe("greenhouse");
  });

  it("counts a field ok if ANY pass filled it (local miss, later hit)", () => {
    const fields = [f("a", "Country", "country")];
    const reports = [rep("a", false, "No option matches")]; // local pass missed
    const outcomes = [{ fieldId: "a", ok: true }]; // combobox/AI pass filled it

    const t = buildAutofillTelemetry(fields, { host: "h", url: "u", atsType: "" }, { reports, outcomes });

    expect(t.filled).toBe(1);
    expect(t.failed).toBe(0);
    expect(t.failedFields).toEqual([]);
  });

  it("threads a combobox/driver outcome's reason into failedFields", () => {
    // Previously outcome failures were logged with an empty reason (""), the
    // exact SF/Workday dropdown blind spot. The reason must now survive.
    const fields = [f("g", "Please state your gender:", "eeoGender")];
    const outcomes = [{ fieldId: "g", ok: false, reason: "Selection didn't stick. Select it manually" }];
    const t = buildAutofillTelemetry(
      fields,
      { host: "career2.successfactors.eu", url: "u", atsType: "successfactors" },
      { reports: [], outcomes }
    );
    expect(t.failed).toBe(1);
    expect(t.failedFields[0].reason).toBe("Selection didn't stick. Select it manually");
  });

  it("emits only label/category/reason, never user values", () => {
    const fields = [f("a", "Some Question")];
    const t = buildAutofillTelemetry(
      fields,
      { host: "h", url: "u", atsType: "" },
      { reports: [rep("a", false, "cannot be scripted")], outcomes: [] }
    );
    expect(Object.keys(t.failedFields[0]).sort()).toEqual(["category", "label", "reason"]);
  });
});

describe("successes are recorded too", () => {
  it("logs a per-field record for a field that filled, not just for failures", () => {
    const fields = [f("a", "First Name", "firstName")];
    const t = buildAutofillTelemetry(
      fields,
      { host: "h", url: "u", atsType: "" },
      {
        reports: [rep("a", true)],
        outcomes: [],
        provenance: new Map([["a", { tier: "profile" }]]),
        intended: [{ fieldId: "a", value: "Ada" }],
        observed: [{ fieldId: "a", value: "Ada" }],
      }
    );
    expect(t.fieldOutcomes).toEqual([
      {
        label: "First Name",
        category: "firstName",
        tier: "profile",
        pass: "",
        expectedValuePresent: true,
        observedValuePresent: true,
        outcome: "filled",
      },
    ]);
  });

  it("names the tier and pass responsible for a value", () => {
    const fields = [f("a", "Are you 18 or older?")];
    const t = buildAutofillTelemetry(
      fields,
      { host: "h", url: "u", atsType: "" },
      {
        reports: [rep("a", true)],
        outcomes: [],
        provenance: new Map([["a", { tier: "backend", pass: "rule" }]]),
        intended: [{ fieldId: "a", value: "No" }],
        observed: [{ fieldId: "a", value: "No" }],
      }
    );
    expect(t.fieldOutcomes?.[0].tier).toBe("backend");
    expect(t.fieldOutcomes?.[0].pass).toBe("rule");
  });

  it("carries no answer text in any per-field record", () => {
    const t = buildAutofillTelemetry(
      [f("a", "Salary expectation")],
      { host: "h", url: "u", atsType: "" },
      {
        reports: [rep("a", true)],
        outcomes: [],
        intended: [{ fieldId: "a", value: "SECRET-90000" }],
        observed: [{ fieldId: "a", value: "SECRET-90000" }],
      }
    );
    expect(JSON.stringify(t)).not.toContain("SECRET");
  });

  it("records a gate drop as its own outcome, with the reason", () => {
    const t = buildAutofillTelemetry(
      [f("a", "Are you 18 or older?")],
      { host: "h", url: "u", atsType: "" },
      {
        reports: [],
        outcomes: [],
        dropped: [{ fieldId: "a", reason: "contradicts_profile:age_gate", source: "rule" }],
      }
    );
    expect(t.fieldOutcomes?.[0].outcome).toBe("dropped");
    expect(t.fieldOutcomes?.[0].reason).toBe("contradicts_profile:age_gate");
    expect(t.failedFields[0].reason).toBe("contradicts_profile:age_gate");
  });
});

describe("revertedFields", () => {
  it("a self-ID answer the widget took in the list's own words holds (Gusto, live, round 8)", () => {
    // "Prefer not to say" became "I don't wish to answer", "Female" "Woman":
    // the same answer, reported as reverted.
    const same = (want: string, got: string, demographic: "eeoSexualOrientation" | "eeoGender") =>
      revertedFields([{ fieldId: "a", value: want }], [{ fieldId: "a", value: got, choice: true, demographic }], new Set(["a"])).length === 0;
    expect(same("Prefer not to say", "I don't wish to answer", "eeoSexualOrientation")).toBe(true);
    expect(same("Female", "Woman", "eeoGender")).toBe(true);
    // Another answer is still a change.
    expect(same("Female", "Man", "eeoGender")).toBe(false);
  });

  it("catches a value the framework cleared after the write verified", () => {
    const out = revertedFields(
      [{ fieldId: "a", value: "Yes" }],
      [{ fieldId: "a", value: "" }],
      new Set(["a"])
    );
    expect(out).toEqual([{ fieldId: "a", cleared: true }]);
  });

  it("catches a value the framework replaced with a different one", () => {
    const out = revertedFields(
      [{ fieldId: "a", value: "Yes" }],
      [{ fieldId: "a", value: "Select One" }],
      new Set(["a"])
    );
    expect(out).toEqual([{ fieldId: "a", cleared: false }]);
  });

  it("tolerates the control normalizing case, spacing or punctuation", () => {
    const out = revertedFields(
      [{ fieldId: "a", value: "Yes, I am" }],
      [{ fieldId: "a", value: "yes i am" }],
      new Set(["a"])
    );
    expect(out).toEqual([]);
  });

  it("a date's part holds its part of the date written (Workday replica, 2026-10-05)", () => {
    // Each part was asked to hold the whole date; it reads back its own part.
    const same = (want: string, got: string) => revertedFields([{ fieldId: "a", value: want }], [{ fieldId: "a", value: got }], new Set(["a"])).length === 0;
    expect(same("Jun 2012", "6")).toBe(true);
    expect(same("Jun 2012", "06")).toBe(true);
    expect(same("Jun 2012", "2012")).toBe(true);
    expect(same("2016-05", "5")).toBe(true);
    expect(same("Dec 2022", "12")).toBe(true);
    // Another month or year is still a change.
    expect(same("Jun 2012", "7")).toBe(false);
    expect(same("Jun 2012", "2013")).toBe(false);
  });

  it("an option the shared matcher picked for the answer holds it (Workday replica, 2026-10-05)", () => {
    const same = (want: string, got: string, choice = true) =>
      revertedFields([{ fieldId: "a", value: want }], [{ fieldId: "a", value: got, choice }], new Set(["a"])).length === 0;
    // In a text box a near-match is a change: a street number edited.
    expect(same("123 Main Street", "125 Main Street", false)).toBe(false);
    // The profile's number, read back as the bucket it fell in.
    expect(same("6", "5-7 years")).toBe(true);
    // The profile's wording against the option's ("of a protected" / "of protected").
    expect(same("I identify as one or more of the classifications of a protected veteran", "I identify as one or more of the classifications of protected veteran")).toBe(true);
    // An answer of the other polarity is still a change.
    expect(same("I am not a protected veteran", "I identify as one or more of the classifications of protected veteran")).toBe(false);
    expect(same("6", "1-3 years")).toBe(false);
  });

  it("does not report a field whose write already failed", () => {
    // That is a failure, not a revert, conflating them hides the interesting
    // case behind the ordinary one.
    const out = revertedFields(
      [{ fieldId: "a", value: "Yes" }],
      [{ fieldId: "a", value: "" }],
      new Set()
    );
    expect(out).toEqual([]);
  });

  it("does not report a field that left the DOM between fill and re-scan", () => {
    const out = revertedFields([{ fieldId: "a", value: "Yes" }], [], new Set(["a"]));
    expect(out).toEqual([]);
  });

  it("a place typeahead keeping its own spelling of the place typed is no revert (Superhuman on Ashby)", () => {
    // Live 2026-10-03: "Montréal, QC" typed, "Montreal, Quebec, Canada" chosen.
    const out = revertedFields(
      [{ fieldId: "a", value: "Montréal, QC" }],
      [{ fieldId: "a", value: "Montreal, Quebec, Canada" }],
      new Set(["a"])
    );
    expect(out).toEqual([]);
  });

  it("an accent the control drops is no revert", () => {
    expect(revertedFields([{ fieldId: "a", value: "Université de Montréal" }], [{ fieldId: "a", value: "Universite de Montreal" }], new Set(["a"]))).toEqual([]);
  });

  it("a checkbox left unticked holds no value: 'no' written is no revert, and nothing to re-write", () => {
    // Superhuman's "Still Student?" for a graduate: proposed "no", read back ""
    // and re-written as "cleared" (live 2026-10-03).
    expect(revertedFields([{ fieldId: "a", value: "no" }], [{ fieldId: "a", value: "", checkbox: true }], new Set(["a"]))).toEqual([]);
    // A text field that held "No" and went blank WAS cleared.
    expect(revertedFields([{ fieldId: "a", value: "No" }], [{ fieldId: "a", value: "" }], new Set(["a"]))).toEqual([{ fieldId: "a", cleared: true }]);
    // A ticked box the page unticked was cleared too.
    expect(revertedFields([{ fieldId: "a", value: "yes" }], [{ fieldId: "a", value: "", checkbox: true }], new Set(["a"]))).toEqual([{ fieldId: "a", cleared: true }]);
  });

  it("a box ticked as asked reads 'checked': no revert (Ramp's 'Still Student?', live 2026-10-03)", () => {
    const box = (value: string, now: string) => revertedFields([{ fieldId: "a", value }], [{ fieldId: "a", value: now, checkbox: true }], new Set(["a"]));
    expect(box("yes", "checked")).toEqual([]);
    expect(box("no", "checked")).toEqual([{ fieldId: "a", cleared: false }]);
  });

  it("an option worded differently but saying the same yes or no is no revert (Brex, SpaceX)", () => {
    // Live 2026-10-03: "Yes" picked "Consent"; "None" picked "Never held a clearance".
    const same = (value: string, now: string) => revertedFields([{ fieldId: "a", value }], [{ fieldId: "a", value: now }], new Set(["a"]));
    expect(same("Yes", "Consent")).toEqual([]);
    expect(same("None", "Never held a clearance")).toEqual([]);
    expect(same("Yes", "No")).toEqual([{ fieldId: "a", cleared: false }]);
  });

  it("a native date input reads back the same day in ISO: no revert (Paylocity, live 2026-10-03)", () => {
    const day = (value: string, now: string) => revertedFields([{ fieldId: "a", value }], [{ fieldId: "a", value: now }], new Set(["a"]));
    expect(day("01/04/2027", "2027-01-04")).toEqual([]);
    expect(day("01/04/2027", "2027-01-05")).toEqual([{ fieldId: "a", cleared: false }]);
  });

  it("another place of the same name is still a revert", () => {
    const out = revertedFields(
      [{ fieldId: "a", value: "London, ON, Canada" }],
      [{ fieldId: "a", value: "London, England, United Kingdom" }],
      new Set(["a"])
    );
    expect(out).toEqual([{ fieldId: "a", cleared: false }]);
  });
});

describe("a revert is a failure, not a fill", () => {
  it("counts a reverted field as failed and names it in the record", () => {
    const t = buildAutofillTelemetry(
      [f("a", "Are you 18 or older?")],
      { host: "h", url: "u", atsType: "" },
      {
        reports: [rep("a", true)], // the write verified at write time
        outcomes: [],
        provenance: new Map([["a", { tier: "backend", pass: "ai" }]]),
        intended: [{ fieldId: "a", value: "Yes" }],
        observed: [{ fieldId: "a", value: "" }], // …and the page does not hold it
      }
    );
    expect(t.filled).toBe(0);
    expect(t.failed).toBe(1);
    expect(t.reverted).toBe(1);
    expect(t.fieldOutcomes?.[0].outcome).toBe("reverted");
    expect(t.fieldOutcomes?.[0].observedValuePresent).toBe(false);
    expect(t.failedFields[0].reason).toBe("value_cleared_after_write");
  });

  it("reports a value the page swapped for something else", () => {
    const t = buildAutofillTelemetry(
      [f("a", "Country", "country")],
      { host: "h", url: "u", atsType: "" },
      {
        reports: [rep("a", true)],
        outcomes: [],
        intended: [{ fieldId: "a", value: "Canada" }],
        observed: [{ fieldId: "a", value: "United States" }],
      }
    );
    expect(t.fieldOutcomes?.[0].outcome).toBe("reverted");
    expect(t.fieldOutcomes?.[0].observedValuePresent).toBe(true);
    expect(t.failedFields[0].reason).toBe("value_changed_after_write");
  });

  it("a phone picker showing the dial code of the country written is no revert (Greenhouse, live 2026-10-05)", () => {
    // Prod telemetry 2026-09-28 and 2026-10-03 logged "Country: value_changed_after_write"
    // for "+1" on pages that held the right country.
    const run = (written: string, shown: string) =>
      buildAutofillTelemetry(
        [f("a", "Country", "country")],
        { host: "h", url: "u", atsType: "" },
        { reports: [rep("a", true)], outcomes: [], intended: [{ fieldId: "a", value: written }], observed: [{ fieldId: "a", value: shown }] }
      ).fieldOutcomes?.[0].outcome;
    expect(run("Canada", "+1")).toBe("filled");
    expect(run("United States", "+1")).toBe("filled");
    expect(run("United Kingdom", "+44")).toBe("filled");
    expect(run("Canada", "+44")).toBe("reverted");
  });

  it("leaves the counts alone when no re-scan was possible", () => {
    const t = buildAutofillTelemetry(
      [f("a", "First Name", "firstName")],
      { host: "h", url: "u", atsType: "" },
      { reports: [rep("a", true)], outcomes: [], intended: [{ fieldId: "a", value: "Ada" }] }
    );
    expect(t.filled).toBe(1);
    expect(t.reverted).toBe(0);
  });
});
