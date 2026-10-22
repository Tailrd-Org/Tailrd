// chrome-extension/test/leverAdapter.test.ts
import { describe, it, expect, beforeEach, vi } from "vitest";
import { leverAdapter, pickLocationSuggestion } from "../src/content/adapters/lever";
import type { FieldContext, FillContext } from "../src/content/adapters/types";
import type { RuntimeControl } from "../src/content/formScanner";

beforeEach(() => {
  document.body.innerHTML = "";
});

function fieldCtx(attrs: Record<string, string>): FieldContext {
  const el = document.createElement("input");
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  document.body.append(el);
  return { el, signals: {} as FieldContext["signals"], controlType: "text" };
}

const generic = { category: "unknown" as const, confidence: 0, sensitive: false };

describe("leverAdapter.match", () => {
  it("matches lever.co hosts (incl. EU) but not look-alikes", () => {
    expect(leverAdapter.match("jobs.lever.co", "https://jobs.lever.co/acme/x/apply")).toBe(true);
    expect(leverAdapter.match("jobs.eu.lever.co", "")).toBe(true);
    expect(leverAdapter.match("lever.co.evil.com", "")).toBe(false);
    expect(leverAdapter.match("example.com", "")).toBe(false);
  });
});

describe("leverAdapter.classify", () => {
  it("maps a current-company / org field", () => {
    expect(leverAdapter.classify!(fieldCtx({ name: "org" }), generic)?.category).toBe("currentCompany");
    expect(leverAdapter.classify!(fieldCtx({ name: "current-company" }), generic)?.category).toBe("currentCompany");
  });
  it("declines unrelated fields", () => {
    expect(leverAdapter.classify!(fieldCtx({ name: "cards[abc][field0]" }), generic)).toBeUndefined();
  });
});

describe("leverAdapter location typeahead", () => {
  function mountLocation(): { input: HTMLInputElement; hidden: HTMLInputElement } {
    const wrap = document.createElement("div");
    wrap.className = "application-question";
    wrap.innerHTML =
      '<input type="text" name="location" data-qa="location-input" autocomplete="off">' +
      '<input type="hidden" name="selectedLocation" value="">';
    document.body.append(wrap);
    return {
      input: wrap.querySelector('[data-qa="location-input"]') as HTMLInputElement,
      hidden: wrap.querySelector('input[name="selectedLocation"]') as HTMLInputElement,
    };
  }
  const fill = (el: HTMLInputElement, value: string): FillContext => ({
    control: { id: "x", controlType: "text", el } as RuntimeControl,
    value,
    el,
  });

  it("fills the visible input AND the hidden selectedLocation Lever reads", async () => {
    const { input, hidden } = mountLocation();
    const op = leverAdapter.fillOperation!(fill(input, "Ottawa, ON, Canada"));
    expect(op).toBeInstanceOf(Promise); // adapter claims the field
    const result = await op!;
    expect(result.filled).toBe(true);
    expect(input.value).toBe("Ottawa, ON, Canada");
    expect(hidden.value).toBe(JSON.stringify({ name: "Ottawa, ON, Canada" }));
  });

  it("declines (undefined) a plain text field so the generic writer handles it", () => {
    const el = document.createElement("input");
    el.type = "text";
    el.name = "cards[abc][field0]";
    document.body.append(el);
    expect(leverAdapter.fillOperation!(fill(el, "hello"))).toBeUndefined();
  });
});

/**
 * Lever's live "Current location" (2026-10-03, three real forms): a typeahead
 * that searches as you type, renders suggestions into `.dropdown-results`, and
 * CLEARS an entry that was not picked from that list. The text+hidden-JSON write
 * above left the field empty on every live page; the fill now types the city and
 * clicks Lever's own suggestion.
 */
describe("leverAdapter location typeahead (live markup)", () => {
  function mountLiveLocation(suggestionsFor: (q: string) => string[], delayMs = 50) {
    const wrap = document.createElement("li");
    wrap.className = "application-question";
    wrap.innerHTML =
      '<div class="application-field"><input class="location-input" data-qa="location-input" id="location-input" type="text" name="location">' +
      '<input id="selected-location" type="hidden" name="selectedLocation">' +
      '<div class="dropdown-container"><div class="dropdown-results"></div></div></div>';
    document.body.append(wrap);
    const input = wrap.querySelector("#location-input") as HTMLInputElement;
    const hidden = wrap.querySelector("#selected-location") as HTMLInputElement;
    const results = wrap.querySelector(".dropdown-results") as HTMLElement;
    input.addEventListener("input", () => {
      const q = input.value;
      setTimeout(() => {
        results.innerHTML = "";
        for (const s of suggestionsFor(q)) {
          const d = document.createElement("div");
          d.className = "dropdown-location";
          d.textContent = s;
          d.addEventListener("click", () => {
            input.value = s;
            hidden.value = JSON.stringify({ name: s });
            results.innerHTML = "";
          });
          results.append(d);
        }
      }, delayMs);
    });
    return { input, hidden };
  }
  const fillCtx = (el: HTMLInputElement, value: string): FillContext => ({
    control: { id: "loc", controlType: "text", el } as RuntimeControl,
    value,
    el,
  });

  it("types the city and clicks Lever's matching suggestion", async () => {
    const { input, hidden } = mountLiveLocation((q) =>
      q.toLowerCase().startsWith("toronto") ? ["Toronto, Ontario, Canada", "Toronto, Ohio, United States"] : []
    );
    const result = await leverAdapter.fillOperation!(fillCtx(input, "Toronto, ON, Canada"))!;
    expect(result.filled).toBe(true);
    expect(input.value).toBe("Toronto, Ontario, Canada");
    expect(JSON.parse(hidden.value).name).toBe("Toronto, Ontario, Canada");
  });

  it("waits out a slow search (SEP and Wattpad live, 2026-10-08: the list came after the wait)", async () => {
    vi.useFakeTimers();
    try {
      const { input, hidden } = mountLiveLocation((q) => (q.toLowerCase().startsWith("boston") ? ["Boston, MA, USA"] : []), 5000);
      const pending = leverAdapter.fillOperation!(fillCtx(input, "Boston, MA, USA"))!;
      await vi.advanceTimersByTimeAsync(9000);
      const result = await pending;
      expect(result.filled).toBe(true);
      expect(input.value).toBe("Boston, MA, USA");
      expect(JSON.parse(hidden.value).name).toBe("Boston, MA, USA");
    } finally {
      vi.useRealTimers();
    }
  });

  it("waits out a cold search too (round 7: Waabi's Toronto went blank once; Lever's first search for a city answered after more than 5 s)", async () => {
    vi.useFakeTimers();
    try {
      const { input, hidden } = mountLiveLocation((q) => (q.toLowerCase().startsWith("toronto") ? ["Toronto, ON, CAN"] : []), 10000);
      const pending = leverAdapter.fillOperation!(fillCtx(input, "Toronto, ON, Canada"))!;
      await vi.advanceTimersByTimeAsync(13000);
      const result = await pending;
      expect(result.filled).toBe(true);
      expect(JSON.parse(hidden.value).name).toBe("Toronto, ON, CAN");
    } finally {
      vi.useRealTimers();
    }
  });

  it("refuses an ambiguous list rather than pick the wrong Toronto", async () => {
    const { input, hidden } = mountLiveLocation(() => ["Toronto, Ohio, United States", "Toronto, Kansas, United States"]);
    const result = await leverAdapter.fillOperation!(fillCtx(input, "Toronto, ON, Canada"))!;
    expect(result.filled).toBe(false);
    expect(input.value).toBe("");
    expect(hidden.value).toBe("");
  });
});

describe("pickLocationSuggestion", () => {
  it("one city of that name is enough; several need the region to agree", () => {
    expect(pickLocationSuggestion(["Toronto, Ontario, Canada"], "Toronto")).toBe(0);
    expect(pickLocationSuggestion(["Toronto, Ohio, US", "Toronto, Ontario, Canada"], "Toronto, ON, Canada")).toBe(1);
    expect(pickLocationSuggestion(["Toronto, Ohio, US", "Toronto, Ontario, Canada"], "Toronto")).toBe(-1);
    expect(pickLocationSuggestion(["Torontonian Club"], "Toronto")).toBe(-1);
  });
});

describe("pickLocationSuggestion: Lever's real suggestion format", () => {
  // Verbatim from jobs.lever.co/searchLocations?text=Toronto, 2026-10-03.
  const live = ["Toronto, ON, CAN", "Toronto, OH, USA", "Toronto, Durham, England, GBR", "Toronto, IN, USA"];
  it("matches places, not words: 'Canada'/'Ontario' ↔ 'CAN'/'ON'", () => {
    expect(pickLocationSuggestion(live, "Toronto, ON, Canada")).toBe(0);
    expect(pickLocationSuggestion(live, "Toronto, Ontario, Canada")).toBe(0);
    expect(pickLocationSuggestion(live, "Toronto, Ohio")).toBe(1);
  });
  it("a bare city with several namesakes picks none", () => {
    expect(pickLocationSuggestion(live, "Toronto")).toBe(-1);
  });
  it("one city listed under two districts is still one place: the search's first (Brillio, live, round 7)", () => {
    // Verbatim from jobs.lever.co/searchLocations?text=Bengaluru, 2026-10-20:
    // the two Karnataka entries left the box empty on two runs.
    const bengaluru = ["Bengaluru, Bengaluru Urban, Karnataka, IND", "Bengaluru, Bangalore, Karnataka, IND", "Beṅgalurū, Almel, Vijyāpurā, Karnaatak, IND", "Beṅgalurū, Paramākūdī, Ramānāthpuram, Tamilnaadu, IND"];
    expect(pickLocationSuggestion(bengaluru, "Bengaluru, Karnataka, India")).toBe(0);
    // Namesakes in different regions stay apart.
    expect(pickLocationSuggestion(["Springfield, IL, USA", "Springfield, MO, USA"], "Springfield, United States")).toBe(-1);
  });
});

describe("pickLocationSuggestion: a namesake whose region shares the city's name (Humble Robotics, live, round 8)", () => {
  // Verbatim from jobs.lever.co/searchLocations?text=San%20Jose, 2026-10-22:
  // "San Jose, CA" (US_GREENCARD_STUDENT) left the box empty.
  const live = ["San Jose, San José, CRI", "San Jose, CA, USA", "São José, Braço do Norte, Santa Catarina, BRA", "San Jose, Occidental Mindoro, Mimaropa, PHL", "São José, Santa Catarina, BRA"];
  it("the state the profile names picks the city", () => {
    expect(pickLocationSuggestion(live, "San Jose, CA")).toBe(1);
    expect(pickLocationSuggestion(live, "San Jose, California, United States")).toBe(1);
  });
});
