/**
 * A same-origin child frame's controls are that frame's own: every http(s)
 * frame runs its own content script, which scans them and announces its form
 * to the top frame. The top frame's scan reaches into such frames
 * (deepQueryAll), and while their native inputs fail its instanceof checks,
 * ARIA widgets do not. On iCIMS's login (Schwab, live 2026-10-17) the top frame
 * so took the frame's phone-country combobox as a field of its own: it filled
 * it, kept the flow, and the frame, never adopted, never got the profile.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "vitest";
import { scanPage } from "../src/content/formScanner";
import { stubLayout } from "./helpers/layout";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());
beforeEach(() => {
  document.body.innerHTML = "";
});

function frameWith(html: string, url: string): Document {
  document.body.innerHTML = `<iframe id="icims_content_iframe"></iframe>`;
  const frame = document.getElementById("icims_content_iframe") as HTMLIFrameElement;
  const doc = frame.contentDocument!;
  Object.defineProperty(doc, "URL", { value: url, configurable: true });
  doc.body.innerHTML = html;
  const proto = (frame.contentWindow as unknown as typeof window).HTMLElement.prototype;
  proto.getClientRects = window.HTMLElement.prototype.getClientRects;
  return doc;
}

const LOGIN = `
  <label for="dropdown">Country Code</label>
  <div id="dropdown" role="combobox" aria-haspopup="listbox" aria-expanded="false" tabindex="0">— Make a Selection —</div>
  <div role="textbox" contenteditable="true" aria-label="Notes"></div>`;

describe("the top frame's scan and a same-origin child frame", () => {
  it("leaves a child page's ARIA widgets to that page's own content script", () => {
    frameWith(LOGIN, "https://career-schwab.icims.com/jobs/127949/associate-software-engineer/login?in_iframe=1");
    const { fields } = scanPage({ firstName: "Ji-woo", phone: "+1 617 555 0148", country: "United States" } as never, false);
    expect(fields.map((f) => f.label)).toEqual([]);
  });

  it("still reaches a blank frame no content script runs in (a rich-text editor)", () => {
    frameWith(LOGIN, "about:blank");
    const { fields } = scanPage(null, false);
    expect(fields.length).toBeGreaterThan(0);
  });
});
