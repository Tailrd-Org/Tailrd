import { describe, it, expect, beforeEach, beforeAll, afterAll } from "vitest";
import { findApplyEntry } from "../src/content/applyEntry";
import { workdayAdapter } from "../src/content/adapters/workday";
import type { SiteAdapter } from "../src/content/adapters/types";
import { stubLayout } from "./helpers/layout";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("findApplyEntry", () => {
  it("finds the posting's Apply button by anchored text", () => {
    document.body.innerHTML = `
      <p>To apply for this role you should read the description.</p>
      <button id="go">Apply Now</button>`;
    const entry = findApplyEntry(document, null);
    expect(entry?.el.id).toBe("go");
    expect(entry?.label).toBe("Apply Now");
    expect(entry?.fromAdapter).toBe(false);
  });

  it("never matches body copy containing the word apply", () => {
    document.body.innerHTML = `
      <a href="#">Learn how to apply for jobs like this one and stand out</a>
      <button>Save job</button>`;
    expect(findApplyEntry(document, null)).toBeNull();
  });

  it("prefers Apply Manually on the chooser and skips the bypass options", () => {
    document.body.innerHTML = `
      <a href="/apply/autofillWithResume">Autofill with Resume</a>
      <a href="/apply/applyManually" id="manual">Apply Manually</a>
      <a href="/apply/useMyLastApplication">Use My Last Application</a>`;
    const entry = findApplyEntry(document, null);
    expect(entry?.el.id).toBe("manual");
    expect(entry?.label).toBe("Apply Manually");
  });

  it("returns null when only bypass options exist", () => {
    document.body.innerHTML = `
      <a href="/apply/autofillWithResume">Autofill with Resume</a>
      <a href="#">Apply with LinkedIn</a>`;
    expect(findApplyEntry(document, null)).toBeNull();
  });

  it("ignores disabled and hidden buttons", () => {
    document.body.innerHTML = `
      <button disabled>Apply</button>
      <button aria-disabled="true">Apply Now</button>
      <button style="display:none">Apply</button>`;
    expect(findApplyEntry(document, null)).toBeNull();
  });

  it("falls back to Continue Application (a Workday draft in progress)", () => {
    document.body.innerHTML = `<button id="cont">Continue Application</button>`;
    expect(findApplyEntry(document, null)?.el.id).toBe("cont");
  });

  it("uses the Workday adapter's automation-id and marks it fromAdapter", () => {
    document.body.innerHTML = `
      <a data-automation-id="adventureButton" role="button" id="wd">Apply</a>`;
    const entry = findApplyEntry(document, workdayAdapter);
    expect(entry?.el.id).toBe("wd");
    expect(entry?.fromAdapter).toBe(true);
  });

  it("survives an adapter hook that throws", () => {
    const broken: SiteAdapter = {
      id: "broken",
      match: () => true,
      entryButton: () => {
        throw new Error("boom");
      },
    };
    document.body.innerHTML = `<button id="go">Apply</button>`;
    expect(findApplyEntry(document, broken)?.el.id).toBe("go");
  });

  it("reads the visible text when the aria-label names the job (Dayforce)", () => {
    document.body.innerHTML = `
      <button id="go" test-id="apply-button" aria-label="Apply for Software Designer&nbsp;" type="button"><span>Apply</span></button>`;
    const entry = findApplyEntry(document, null);
    expect(entry?.el.id).toBe("go");
    expect(entry?.label).toBe("Apply");
  });

  it("still skips a bypass option named only in its aria-label", () => {
    document.body.innerHTML = `<button aria-label="Apply with LinkedIn">Apply</button>`;
    expect(findApplyEntry(document, null)).toBeNull();
  });

  it("takes Dayforce's guest path over signing in", () => {
    document.body.innerHTML = `
      <h2>Apply without an Account or Already Have an Account?</h2>
      <button type="button" id="guest">Apply without an Account</button>
      <button type="button">Sign In</button>
      <a href="#">Create one now.</a>`;
    expect(findApplyEntry(document, null)?.el.id).toBe("guest");
  });

  it("knows iCIMS's 'Apply for this job online' (round 5 batch C: never opened)", () => {
    // Every iCIMS posting tonight showed the job with this link and the flow
    // found no entry: the anchored text stopped at "job".
    document.body.innerHTML = `
      <p>Apply online by Friday if you can.</p>
      <a class="iCIMS_ApplyOnlineButton iCIMS_PrimaryButton" id="go" href="https://careers-x.icims.com/jobs/1/job/login?mode=apply">Apply for this job online</a>`;
    expect(findApplyEntry(document, null)?.el.id).toBe("go");
    document.body.innerHTML = `<button id="go">Apply online</button>`;
    expect(findApplyEntry(document, null)?.el.id).toBe("go");
  });

  // The real iCIMS button (DocuSign, live 2026-10-17): a long and a short label
  // side by side, the short one hidden by the page's stylesheet, so its text
  // runs together as "Apply for this job onlineApply" and the anchored match
  // never saw it. The title says what the button shows.
  const ICIMS_APPLY = `
    <a id="go" href="https://uscareers-x.icims.com/jobs/30499/software-engineer-intern/job?mode=apply&amp;apply=yes&amp;in_iframe=1&amp;hashed=-1834411579" class="iCIMS_Anchor iCIMS_Action_Button iCIMS_ApplyOnlineButton iCIMS_PrimaryButton" title="Apply for this job online"><div class="iCIMS_Action_ButtonText"><span class="iCIMS_LongLabel">Apply for this job online</span><span class="iCIMS_ShortLabel" style="display:none">Apply</span></div></a>`;

  it("reads iCIMS's real button: a hidden short label beside the long one (live 2026-10-17)", () => {
    document.body.innerHTML = ICIMS_APPLY;
    const entry = findApplyEntry(document, null);
    expect(entry?.el.id).toBe("go");
    expect(entry?.label).toBe("Apply for this job online");
  });

  it("reads a button by what it shows when no title says it", () => {
    document.body.innerHTML = ICIMS_APPLY.replace(/ title="[^"]*"/, "");
    expect(findApplyEntry(document, null)?.label).toBe("Apply for this job online");
  });

  it("never takes a title that is a sentence about applying", () => {
    document.body.innerHTML = `<a id="go" href="/jobs" title="Apply for this job online">See all jobs</a>`;
    expect(findApplyEntry(document, null)).toBeNull();
  });

  it("finds iCIMS's button inside the posting's same-origin frame (live 2026-10-17)", () => {
    document.body.innerHTML = `<iframe id="icims_content_iframe"></iframe>`;
    const frame = document.getElementById("icims_content_iframe") as HTMLIFrameElement;
    const doc = frame.contentDocument!;
    doc.body.innerHTML = ICIMS_APPLY;
    const frameProto = (frame.contentWindow as unknown as typeof window).HTMLElement.prototype;
    const original = frameProto.getClientRects;
    frameProto.getClientRects = window.HTMLElement.prototype.getClientRects;
    try {
      const entry = findApplyEntry(document, null);
      expect(entry?.el).toBe(doc.getElementById("go"));
      expect(entry?.label).toBe("Apply for this job online");
    } finally {
      frameProto.getClientRects = original;
    }
  });

  it("reads past a decorative arrow: 'Apply now »' (L3Harris on SuccessFactors, round 5 batch C)", () => {
    document.body.innerHTML = `
      <a class="btn btn-primary apply dialogApplyBtn" id="go" href="/talentcommunity/apply/1432400300/?locale=en_US">Apply now »</a>`;
    expect(findApplyEntry(document, null)?.el.id).toBe("go");
    for (const text of ["Apply ›", "Apply now →", "Postuler maintenant »"]) {
      document.body.innerHTML = `<a id="go" href="/apply">${text}</a>`;
      expect(findApplyEntry(document, null)?.el.id).toBe("go");
    }
  });

  it("knows the French Apply of a Jobvite posting, never Apply later", () => {
    document.body.innerHTML = `
      <a class="jv-button jv-button-apply-later" href="mailto:?subject=Postuler">Postuler plus tard</a>
      <a class="jv-button jv-button-primary jv-button-apply" id="go" href="/acpm/job/x/apply">
        Poser sa candidature
      </a>`;
    const entry = findApplyEntry(document, null);
    expect(entry?.el.id).toBe("go");
    expect(entry?.label).toBe("Poser sa candidature");
  });
});

/**
 * A closed Breezy posting sends its /apply to the company's openings (Vagaro,
 * regression 2026-10-17): 39 jobs, each with its own "Apply". The flow took
 * the first, a Director of DevOps role in Ahmedabad, and filled that
 * application. A list of postings offers no entry; one posting's two Apply
 * buttons (top and bottom, or its page and its /apply) still do.
 */
describe("a list of postings is no posting", () => {
  const position = (slug: string, title: string, place: string): string =>
    `<li class="position transition"><ul class="position-wrap pad-b-50"><li class="position-details flex-0">` +
    `<a href="/p/${slug}" title="Apply"><h2>${title}</h2><ul class="meta"><li class="location"><span>${place}</span></li></ul>` +
    `<button class="button apply polygot button-full bzyButtonColor">Apply</button></a></li>` +
    `<li class="position-actions"><a href="/p/${slug}" title="Apply"><button class="button apply polygot button-right bzyButtonColor">Apply</button></a></li></ul></li>`;

  it("offers no Apply on Breezy's openings (Vagaro)", () => {
    document.body.innerHTML =
      `<h1>Openings at Vagaro</h1><ul class="positions">` +
      position("d1ade44fbc28-director-of-devops-sre-infrastructure", "Director of DevOps, SRE &amp; Infrastructure", "Ahmedabad, IN") +
      position("d8c61acc6349-director-software-engineering", "Director- Software Engineering", "Ahmedabad, IN") +
      `</ul>`;
    expect(findApplyEntry(document, null)).toBeNull();
  });

  it("still takes a posting's own Apply, at its top and its bottom", () => {
    document.body.innerHTML =
      `<a id="top" href="/p/1a62d98b9483-software-engineer/apply">Apply</a><p>The role…</p>` +
      `<a id="bottom" href="/p/1a62d98b9483-software-engineer/apply?source=bottom#form">Apply</a>`;
    expect(findApplyEntry(document, null)?.el.id).toBe("top");
    document.body.innerHTML = `<a id="page" href="/p/1a62d98b9483-software-engineer">Apply</a><a href="/p/1a62d98b9483-software-engineer/apply">Apply now</a>`;
    expect(findApplyEntry(document, null)?.el.id).toBe("page");
  });
});
