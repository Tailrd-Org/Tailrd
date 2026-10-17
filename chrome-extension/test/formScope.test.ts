import { describe, it, expect, beforeEach } from "vitest";
import { resolveFormScope, filterToScope, loneFormScope, type ScopeEntry } from "../src/content/formScope";
import type { DetectedField, FieldCategory } from "../src/shared/types";

beforeEach(() => {
  document.body.innerHTML = "";
});

function field(id: string, category: FieldCategory): DetectedField {
  return {
    id, category, confidence: 0.9, label: id, controlType: "text",
    required: false, proposedValue: null, fillable: true, sensitive: false,
  };
}

function entry(id: string, category: FieldCategory, el: HTMLElement): ScopeEntry {
  return { field: field(id, category), el };
}

describe("resolveFormScope", () => {
  it("picks the <form> containing all recognized fields", () => {
    document.body.innerHTML = `
      <div id="noise"><select id="switcher"><option>EN</option></select></div>
      <form id="app">
        <input id="fn" /><input id="ln" /><input id="em" />
      </form>`;
    const entries = [
      entry("1", "firstName", document.getElementById("fn")!),
      entry("2", "lastName", document.getElementById("ln")!),
      entry("3", "email", document.getElementById("em")!),
      entry("4", "unknown", document.getElementById("switcher")!),
    ];
    expect(resolveFormScope(entries)?.id).toBe("app");
  });

  it("picks the deepest candidate holding >= 80% of recognized fields", () => {
    // main wraps everything; the inner form holds 4 of 5 recognized (80%).
    document.body.innerHTML = `
      <main id="m">
        <input id="stray" />
        <form id="app"><input id="a"/><input id="b"/><input id="c"/><input id="d"/></form>
      </main>`;
    const entries = [
      entry("1", "firstName", document.getElementById("a")!),
      entry("2", "lastName", document.getElementById("b")!),
      entry("3", "email", document.getElementById("c")!),
      entry("4", "phone", document.getElementById("d")!),
      entry("5", "location", document.getElementById("stray")!),
    ];
    expect(resolveFormScope(entries)?.id).toBe("app");
  });

  it("falls back to the LCA when there is no <form> or main", () => {
    document.body.innerHTML = `
      <div><div id="wrap">
        <div><input id="a" /></div><div><input id="b" /></div>
      </div></div>
      <div id="outside"><input id="x" type="email" /></div>`;
    const entries = [
      entry("1", "firstName", document.getElementById("a")!),
      entry("2", "lastName", document.getElementById("b")!),
      entry("3", "unknown", document.getElementById("x")!),
    ];
    // LCA of the two recognized fields is #wrap (body/html are never candidates).
    expect(resolveFormScope(entries)?.id).toBe("wrap");
  });

  it("returns null when fields scatter with no qualifying container", () => {
    // Two recognized fields whose LCA is <body> (excluded), no form/main.
    document.body.innerHTML = `<div><input id="a" /></div><div><input id="b" /></div>`;
    const entries = [
      entry("1", "firstName", document.getElementById("a")!),
      entry("2", "lastName", document.getElementById("b")!),
    ];
    expect(resolveFormScope(entries)).toBeNull();
  });

  it("returns null with fewer than 2 recognized fields", () => {
    document.body.innerHTML = `<form id="f"><input id="a" /></form>`;
    expect(resolveFormScope([entry("1", "email", document.getElementById("a")!)])).toBeNull();
  });

  it("keeps a shadow-hosted field inside the scope (composed containment)", () => {
    document.body.innerHTML = `<form id="app"><input id="a"/><input id="b"/><div id="host"></div></form>`;
    const root = document.getElementById("host")!.attachShadow({ mode: "open" });
    const shadowInput = document.createElement("input");
    root.appendChild(shadowInput);
    const entries = [
      entry("1", "firstName", document.getElementById("a")!),
      entry("2", "lastName", document.getElementById("b")!),
      entry("3", "phone", shadowInput),
    ];
    const scope = resolveFormScope(entries)!;
    expect(scope.id).toBe("app");
    expect(filterToScope(entries, scope).map((e) => e.field.id)).toEqual(["1", "2", "3"]);
  });

  it("filterToScope drops entries outside the scope regardless of category", () => {
    // 4 of 5 recognized inside the form = exactly 80%, the form qualifies and
    // the recognized-but-outside newsletter email is dropped with it.
    document.body.innerHTML = `
      <form id="app"><input id="a"/><input id="b"/><input id="c"/><input id="d"/></form>
      <div id="newsletter"><input id="nl" type="email" /></div>`;
    const entries = [
      entry("1", "firstName", document.getElementById("a")!),
      entry("2", "lastName", document.getElementById("b")!),
      entry("3", "phone", document.getElementById("c")!),
      entry("4", "location", document.getElementById("d")!),
      entry("5", "email", document.getElementById("nl")!), // recognized but outside
    ];
    const scope = resolveFormScope(entries)!;
    expect(scope.id).toBe("app");
    expect(filterToScope(entries, scope).map((e) => e.field.id)).toEqual(["1", "2", "3", "4"]);
  });
});

describe("loneFormScope", () => {
  // iCIMS's candidate login (live 2026-10-17): one recognized field (the
  // email) and the privacy notice's box, in their own <form> with its Next.
  // No scope is drawn from one field, so the flow had nowhere to look for Next.
  it("is the lone recognized field's own form when it holds every scanned field", () => {
    document.body.innerHTML = `
      <form id="enterEmailForm"><input id="email" type="email" />
        <div id="gdpr_container"><input id="accept_gdpr" type="checkbox" /></div>
        <input id="enterEmailSubmitButton" type="submit" value="Next" /></form>`;
    const entries = [
      entry("1", "email", document.getElementById("email")!),
      entry("2", "unknown", document.getElementById("accept_gdpr")!),
    ];
    expect(resolveFormScope(entries)).toBeNull();
    expect(loneFormScope(entries)?.id).toBe("enterEmailForm");
  });

  it("is null when a scanned field sits outside that form, or two fields are recognized", () => {
    document.body.innerHTML = `<form id="f"><input id="a" /></form><input id="b" />`;
    const a = entry("1", "email", document.getElementById("a")!);
    expect(loneFormScope([a, entry("2", "unknown", document.getElementById("b")!)])).toBeNull();
    expect(loneFormScope([a, entry("2", "firstName", document.getElementById("b")!)])).toBeNull();
    document.body.innerHTML = `<div><input id="c" /></div>`;
    expect(loneFormScope([entry("1", "email", document.getElementById("c")!)])).toBeNull();
  });
});
