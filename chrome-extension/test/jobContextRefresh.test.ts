// chrome-extension/test/jobContextRefresh.test.ts
/**
 * The job's place and company, read once per URL and again on the same page
 * until the page names them. Greenhouse's job-boards page names its company
 * only in its title ("Job Application for … at Anduril Industries"), and one
 * run in three read the page before the title was there (Anduril, the round 8
 * regression and alone): "Company website" took "Other" over "Anduril
 * Website" for the rest of the fill.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { createJobContextReader } from "../src/content/jobContextRefresh";
import { getResolveContext, setResolveContext } from "../src/content/fieldResolver";

const URL_A = "https://job-boards.greenhouse.io/andurilindustries/jobs/5240165007?gh_jid=5240165007";

beforeEach(() => {
  document.head.innerHTML = "";
  document.body.innerHTML = "";
  document.title = "";
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
});

describe("createJobContextReader", () => {
  it("a company the page names after the first read is read on the next", () => {
    const reader = createJobContextReader();
    document.body.innerHTML = `<div class="job__location">Costa Mesa, California, United States</div>`;
    reader.refresh(document, URL_A);
    expect(getResolveContext().company).toBe("");
    document.title = "Job Application for Software Engineer, Battlespace Awareness at Anduril Industries";
    reader.refresh(document, URL_A);
    expect(getResolveContext().company).toBe("Anduril Industries");
  });

  it("a company already known stays: one adopted from the page around a frame is not replaced", () => {
    const reader = createJobContextReader();
    document.body.innerHTML = `<div class="job__location">Costa Mesa, California, United States</div>`;
    reader.refresh(document, URL_A);
    setResolveContext({ company: "Brex" });
    document.title = "Job Application for Engineer at Greenhouse";
    reader.refresh(document, URL_A);
    expect(getResolveContext().company).toBe("Brex");
  });

  it("an ATS's own name is no company, on a later read too", () => {
    const reader = createJobContextReader();
    reader.refresh(document, URL_A);
    document.title = "Job Application for Engineer at Greenhouse";
    reader.refresh(document, URL_A);
    expect(getResolveContext().company).toBe("");
  });

  it("a new page is read whole, and asks for an outside place when it names no country", () => {
    const reader = createJobContextReader();
    document.title = "Job Application for Engineer at Anduril Industries";
    document.body.innerHTML = `<div class="job__location">Costa Mesa, California, United States</div>`;
    expect(reader.refresh(document, URL_A)).toBe(false);
    expect(getResolveContext()).toMatchObject({ jobCountry: "US", company: "Anduril Industries" });
    expect(reader.url).toBe(URL_A);
    document.title = "Apply";
    document.body.innerHTML = "";
    expect(reader.refresh(document, "https://example.com/apply")).toBe(true);
    expect(getResolveContext()).toMatchObject({ jobCountry: null, company: "" });
  });

  it("a place the page names late is read until found, then kept", () => {
    const reader = createJobContextReader();
    reader.refresh(document, URL_A);
    expect(getResolveContext().jobCountry).toBe(null);
    document.body.innerHTML = `<div class="job__location">Costa Mesa, California, United States</div>`;
    reader.refresh(document, URL_A);
    expect(getResolveContext().jobCountry).toBe("US");
    document.body.innerHTML = `<div class="job__location">Toronto, Ontario, Canada</div>`;
    reader.refresh(document, URL_A);
    expect(getResolveContext().jobCountry).toBe("US");
  });
});
