/**
 * Structural guards on the panel's markup. collectRefs() throws on the first
 * missing selector, and that throw aborts the whole overlay mount, the panel
 * then never opens on any page. These assert that the elements the sign-ins
 * modal and the branded edge tab depend on actually exist in buildHTML().
 */
import { describe, it, expect, beforeAll } from "vitest";
import { buildHTML } from "../src/content/overlay";

let root: HTMLElement;

beforeAll(() => {
  root = document.createElement("div");
  root.innerHTML = buildHTML();
});

describe("saved sign-ins", () => {
  it("is a section row that opens a modal, not a <details> accordion", () => {
    expect(root.querySelector("#ap-section-signins")).not.toBeNull();
    expect(root.querySelector("details#ap-signins")).toBeNull();
  });

  it("exposes the refs collectRefs() queries", () => {
    for (const sel of ["#ap-signins-modal", "#ap-signins-body", "#ap-signins-count"]) {
      expect(root.querySelector(sel), sel).not.toBeNull();
    }
  });

  it("renders the modal as a backdrop that starts hidden", () => {
    const modal = root.querySelector("#ap-signins-modal")!;
    expect(modal.classList.contains("ap-modal-backdrop")).toBe(true);
    expect(modal.classList.contains("visible")).toBe(false);
  });

  it("gives the modal a close button", () => {
    expect(root.querySelector("#ap-signins-close")).not.toBeNull();
  });

  it("keeps the count badge hidden until there is a credential to count", () => {
    expect(root.querySelector<HTMLElement>("#ap-signins-count")!.style.display).toBe("none");
  });
});

describe("unanswered questions", () => {
  it("exposes the refs collectRefs() queries", () => {
    for (const sel of [
      "#ap-gaps-modal",
      "#ap-gaps-body",
      "#ap-gaps-error",
      "#ap-gaps-save",
    ]) {
      expect(root.querySelector(sel), sel).not.toBeNull();
    }
  });

  // The panel card that opened this was removed from the UI. The modal and
  // everything behind it stays; it just has no entry point in the panel.
  it("has no panel card", () => {
    expect(root.querySelector("#ap-gaps-card")).toBeNull();
    expect(root.querySelector("#ap-gaps-text")).toBeNull();
  });

  it("offers Skip alongside Save, so answering is never forced", () => {
    expect(root.querySelector("#ap-gaps-skip")).not.toBeNull();
    expect(root.querySelector("#ap-gaps-close")).not.toBeNull();
  });

  it("starts hidden", () => {
    const modal = root.querySelector("#ap-gaps-modal")!;
    expect(modal.classList.contains("ap-modal-backdrop")).toBe(true);
    expect(modal.classList.contains("visible")).toBe(false);
  });
});

describe("autofill information", () => {
  // The Remembered-answers feature is gone: no tab, and nothing in the panel
  // may offer to recall an answer across applications.
  it("has no Remembered answers category", () => {
    expect(root.querySelector('.ap-modal-sidebar-item[data-cat="remembered"]')).toBeNull();
  });

  it("every sidebar tab renders a real category form", () => {
    const cats = [...root.querySelectorAll<HTMLElement>(".ap-modal-sidebar-item")].map(
      (b) => b.dataset.cat
    );
    expect(cats).toEqual([
      "personal",
      "address",
      "education",
      "experience",
      "skill",
      "preference",
      "eeo",
      "signup",
    ]);
  });
});

describe("edge tab", () => {
  it("carries the brand mark as inline SVG, so no page CSP can block it", () => {
    const tab = root.querySelector(".ap-edge-tab")!;
    const mark = tab.querySelector("svg.ap-edge-mark")!;
    expect(mark).not.toBeNull();
    expect(mark.getAttribute("aria-hidden")).toBe("true");
    expect(tab.querySelector("img")).toBeNull();
  });

  it("puts the inline-SVG lockup, named Tailrd, in the header", () => {
    const lockup = root.querySelector(".ap-brand svg.ap-brand-lockup")!;
    expect(lockup).not.toBeNull();
    expect(lockup.getAttribute("role")).toBe("img");
    expect(lockup.getAttribute("aria-label")).toBe("Tailrd");
  });
});
