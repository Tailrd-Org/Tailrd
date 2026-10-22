import { describe, it, expect, beforeEach } from "vitest";
import {
  classifyWithAdapter,
  resolveAnswerWithAdapter,
  runAdapterOperations,
  tryAdapterOperation,
} from "../src/content/adapters/apply";
import type { SiteAdapter, FieldContext } from "../src/content/adapters/types";
import { finalOutcomes } from "../src/content/telemetry";
import type { RuntimeControl } from "../src/content/formScanner";
import type { UserApplicationProfile } from "../src/shared/types";

beforeEach(() => { document.body.innerHTML = ""; });

function fieldCtx(): FieldContext {
  const el = document.createElement("input");
  document.body.append(el);
  return { el, signals: { label: "", ariaLabel: "", placeholder: "", nameAttr: "", idAttr: "", testId: "", nearby: "", typeHint: "", autocomplete: "" } as FieldContext["signals"], controlType: "text" };
}

const profile = { firstName: "Ada", location: "Ottawa, ON, Canada" } as unknown as UserApplicationProfile;

describe("classifyWithAdapter", () => {
  it("uses the adapter override when provided", () => {
    const adapter = { id: "x", match: () => true, classify: () => ({ category: "github", confidence: 0.9, sensitive: false }) } as SiteAdapter;
    expect(classifyWithAdapter(adapter, fieldCtx()).category).toBe("github");
  });
  it("falls back to generic when the adapter declines (undefined)", () => {
    const adapter = { id: "x", match: () => true, classify: () => undefined } as SiteAdapter;
    // generic classify of an empty input → "unknown"
    expect(classifyWithAdapter(adapter, fieldCtx()).category).toBe("unknown");
  });
  it("falls back to generic when the adapter hook throws", () => {
    const adapter = { id: "x", match: () => true, classify: () => { throw new Error("boom"); } } as SiteAdapter;
    expect(classifyWithAdapter(adapter, fieldCtx()).category).toBe("unknown");
  });
  it("uses generic when there is no adapter", () => {
    expect(classifyWithAdapter(null, fieldCtx()).category).toBe("unknown");
  });
});

describe("resolveAnswerWithAdapter", () => {
  const control = { controlType: "text" as const };
  it("returns null when no profile is loaded", () => {
    expect(resolveAnswerWithAdapter(null, "firstName", null, control, false, document.body)).toBeNull();
  });
  it("uses a non-undefined adapter override (string) over generic", () => {
    const adapter = { id: "x", match: () => true, resolveAnswer: () => "OVERRIDE" } as SiteAdapter;
    expect(resolveAnswerWithAdapter(adapter, "firstName", profile, control, false, document.body)).toBe("OVERRIDE");
  });
  it("returns a null override verbatim (null = no-data answer, not generic)", () => {
    const adapter = { id: "x", match: () => true, resolveAnswer: () => null } as SiteAdapter;
    expect(resolveAnswerWithAdapter(adapter, "firstName", profile, control, false, document.body)).toBeNull();
  });
  it("falls back to generic when the resolveAnswer hook throws", () => {
    const adapter = { id: "x", match: () => true, resolveAnswer: () => { throw new Error("boom"); } } as SiteAdapter;
    expect(resolveAnswerWithAdapter(adapter, "firstName", profile, control, false, document.body)).toBe("Ada");
  });
  it("falls back to generic resolveProfileValue when the adapter declines", () => {
    const adapter = { id: "x", match: () => true, resolveAnswer: () => undefined } as SiteAdapter;
    expect(resolveAnswerWithAdapter(adapter, "firstName", profile, control, false, document.body)).toBe("Ada");
  });
});

describe("runAdapterOperations", () => {
  const ctrl = (id: string): RuntimeControl => ({ id, controlType: "text", el: document.createElement("input") });
  it("routes claimed fields to the adapter and leaves the rest as remaining", async () => {
    const adapter = {
      id: "x", match: () => true,
      fillOperation: (c) => (c.value === "op" ? Promise.resolve({ filled: true }) : undefined),
    } as SiteAdapter;
    const items = [{ fieldId: "a", value: "op" }, { fieldId: "b", value: "generic" }];
    const reg = new Map([["a", ctrl("a")], ["b", ctrl("b")]]);
    const { opOutcomes, remaining } = await runAdapterOperations(adapter, items, (id) => reg.get(id));
    expect(opOutcomes).toEqual([{ fieldId: "a", ok: true }]);
    expect(remaining).toEqual([{ fieldId: "b", value: "generic" }]);
  });
  it("treats a null adapter as all-remaining", async () => {
    const items = [{ fieldId: "a", value: "x" }];
    const reg = new Map([["a", ctrl("a")]]);
    const { opOutcomes, remaining } = await runAdapterOperations(null, items, (id) => reg.get(id));
    expect(opOutcomes).toEqual([]);
    expect(remaining).toEqual(items);
  });
  it("carries the adapter's reason for a field it could not fill into the report", async () => {
    // Lever's location search listed nothing within its wait (Azul, live, round
    // 8's final regression) and the report read "failed" with no reason.
    const why = "Pick your location from Lever's suggestion list.";
    const adapter = {
      id: "x", match: () => true,
      fillOperation: () => Promise.resolve({ filled: false, reason: why }),
    } as SiteAdapter;
    const reg = new Map([["a", ctrl("a")]]);
    const { opOutcomes } = await runAdapterOperations(adapter, [{ fieldId: "a", value: "Cambridge, MA, United States" }], (id) => reg.get(id));
    expect(opOutcomes).toEqual([{ fieldId: "a", ok: false, reason: why }]);
    expect(finalOutcomes([], opOutcomes).reason.get("a")).toBe(why);
  });
  it("a rejected op resolves to {ok:false} and never throws", async () => {
    const adapter = { id: "x", match: () => true, fillOperation: () => Promise.reject(new Error("boom")) } as SiteAdapter;
    const reg = new Map([["a", ctrl("a")]]);
    const { opOutcomes, remaining } = await runAdapterOperations(adapter, [{ fieldId: "a", value: "x" }], (id) => reg.get(id));
    expect(opOutcomes).toEqual([{ fieldId: "a", ok: false }]);
    expect(remaining).toEqual([]);
  });
});

describe("tryAdapterOperation", () => {
  const el = document.createElement("input");
  const ctx = { control: { id: "a", controlType: "text" as const, el }, value: "v", el };
  it("returns undefined when there is no fillOperation hook", () => {
    const adapter = { id: "x", match: () => true } as SiteAdapter;
    expect(tryAdapterOperation(adapter, ctx)).toBeUndefined();
  });
  it("returns undefined when the hook throws synchronously", () => {
    const adapter = { id: "x", match: () => true, fillOperation: () => { throw new Error("boom"); } } as SiteAdapter;
    expect(tryAdapterOperation(adapter, ctx)).toBeUndefined();
  });
});
