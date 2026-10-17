/**
 * A site's own job search and its result controls are never application
 * fields. A closed iCIMS posting (DocuSign, live 2026-10-17) redirected to the
 * employer's job search; Autofill typed the applicant's current title into
 * "Search Keyword, Category or Job Title" (which ran the search) and sent the
 * filters and "Sort by" to the AI.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { isSiteSearch } from "../src/content/siteSearch";

beforeEach(() => {
  document.body.innerHTML = "";
});

const el = (id: string): HTMLElement => document.getElementById(id)!;

describe("isSiteSearch", () => {
  it("knows a job search box by its id or by what it asks", () => {
    document.body.innerHTML = `
      <input id="keyword-search" role="combobox" aria-label="Search Keyword, Category or Job Title">
      <input id="location-search" role="combobox" aria-label="Search Locations">
      <label for="kw">Search jobs</label><input id="kw" name="kw">
      <input id="x1" placeholder="Search by job title or keyword">
      <input id="x2" name="keywords">`;
    for (const id of ["keyword-search", "location-search", "kw", "x1", "x2"]) expect(isSiteSearch(el(id)), id).toBe(true);
  });

  it("knows the result list's own controls", () => {
    document.body.innerHTML = `
      <label for="sort-by">Sort by</label><select id="sort-by"><option>Relevance</option></select>
      <div id="lbl">Items per page</div><div id="pager" role="combobox" aria-labelledby="lbl"></div>`;
    expect(isSiteSearch(el("sort-by"))).toBe(true);
    expect(isSiteSearch(el("pager"))).toBe(true);
  });

  it("never takes an application's own questions", () => {
    document.body.innerHTML = `
      <label for="a">Job title</label><input id="a">
      <label for="b">What are you searching for in your next role?</label><textarea id="b"></textarea>
      <label for="c">Location</label><input id="c" placeholder="Search for a city">
      <label for="d">How Did You Hear About Us?</label><input id="d" type="text" placeholder="Search" data-automation-id="searchBox">
      <label for="e">School</label><input id="e" placeholder="Search schools">
      <label for="f">Sort the following by preference</label><input id="f">`;
    for (const id of ["a", "b", "c", "d", "e", "f"]) expect(isSiteSearch(el(id)), id).toBe(false);
  });
});

describe("the scan of a job search page (DocuSign's, after a closed iCIMS posting)", () => {
  it("surfaces neither the search box nor the result controls; an application field beside them stays", async () => {
    const { scanPage } = await import("../src/content/formScanner");
    const { stubLayout } = await import("./helpers/layout");
    const restore = stubLayout();
    try {
      document.body.innerHTML = `
        <div class="search-bar">
          <input id="keyword-search" role="combobox" aria-label="Search Keyword, Category or Job Title" type="text">
          <input id="location-search" role="combobox" aria-label="Search Locations" type="text">
        </div>
        <label for="sort-by">Sort by</label><select id="sort-by"><option>Relevance</option><option>Date</option></select>
        <form><label for="em">Email</label><input id="em" type="email"></form>`;
      const { fields } = scanPage({ firstName: "Maya", email: "maya@example.com", currentTitle: "Data Engineer" } as never, false);
      const labels = fields.map((f) => f.label);
      expect(labels.some((l) => /search/i.test(l))).toBe(false);
      expect(labels.some((l) => /sort by/i.test(l))).toBe(false);
      expect(fields.some((f) => f.category === "email")).toBe(true);
    } finally {
      restore();
    }
  });
});
