/**
 * Round 5 (2026-10-08, night): open bugs from round 4 and new ground. Each
 * test is a write a live page got wrong, or a blank a stated fact answers;
 * labels and markup are verbatim from the live pages.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import { AutofillReconciler } from "../src/content/reconciler";
import { fillAriaCombobox } from "../src/content/comboboxEngine";
import { revertedFields } from "../src/content/telemetry";
import { holdsNoAnswer } from "../src/content/flowChecks";
import { resolveQuestion, type QuestionInput } from "../src/content/questionResolver";
import { profileFacts } from "../src/content/profileFacts";
import * as P from "./e2e/profiles.mjs";
import type { UserApplicationProfile } from "../src/shared/types";
import { liveControlFor } from "../src/content/staleControl";
import { loadWorkableGuesses, prefilledGuesses, setPageGuesses, workableShortcode } from "../src/content/pageGuesses";
import type { RuntimeControl } from "../src/content/formScanner";
import { SPARSE_CANADIAN, TEST_TODAY } from "./fixtures/profiles";
import { stubLayout } from "./helpers/layout";
import { detectJobPlace } from "../src/content/jobLocation";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());
beforeEach(() => {
  document.body.innerHTML = "";
});

describe("a popup's hide-others mark does not hide the form (Ashby's school menu, Superhuman)", () => {
  // While Ashby's school typeahead is open, Floating UI's "hide others" marks
  // every other field block aria-hidden="true" data-aria-hidden="true" (37
  // nodes, live 2026-10-08), and removes the marks when the menu closes. A
  // scan taken while the first school's menu was open lost the second school,
  // which then failed as "Field no longer found" (regressions 2 and 3).
  const ASHBY_OPEN_MENU = `
    <form>
      <div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry" aria-hidden="true" data-aria-hidden="true">
        <label class="_heading_101oc_53" for="_systemfield_email">Email</label>
        <input id="_systemfield_email" type="email" name="_systemfield_email" />
      </div>
      <div class="_fieldEntry_1e3gg_28">
        <label>School</label>
        <input role="combobox" aria-expanded="true" aria-controls=":r3:" placeholder="Search schools..." value="Univ" />
      </div>
      <div class="_fieldEntry_1e3gg_28" aria-hidden="true" data-aria-hidden="true">
        <label>School</label>
        <input role="combobox" aria-expanded="false" placeholder="Search schools..." />
      </div>
    </form>
    <div data-floating-ui-portal="" aria-hidden="false"><div id=":r3:" role="listbox"><div role="option">Concordia University</div></div></div>`;

  it("a field outside the open menu is still scanned, under its own label", () => {
    document.body.innerHTML = ASHBY_OPEN_MENU;
    const { fields } = scanPage(SPARSE_CANADIAN, false, null);
    const email = fields.find((f) => f.category === "email");
    expect(email?.label).toBe("Email");
    expect(fields.filter((f) => f.label === "School")).toHaveLength(2);
  });

  it("markup hidden from screen readers for good (no hide-others mark) is still skipped", () => {
    document.body.innerHTML = `
      <form>
        <label for="a">Email</label><input id="a" type="email" />
        <div aria-hidden="true"><label for="b">Email</label><input id="b" type="email" /></div>
      </form>`;
    const { fields } = scanPage(SPARSE_CANADIAN, false, null);
    expect(fields.filter((f) => f.category === "email")).toHaveLength(1);
  });

  it("a field hidden for good inside a block a menu marked is still skipped", () => {
    document.body.innerHTML = `
      <form>
        <label for="a">Email</label><input id="a" type="email" />
        <div aria-hidden="true" data-aria-hidden="true">
          <div aria-hidden="true"><label for="b">Email</label><input id="b" type="email" /></div>
        </div>
      </form>`;
    const { fields } = scanPage(SPARSE_CANADIAN, false, null);
    expect(fields.filter((f) => f.category === "email")).toHaveLength(1);
  });
});

describe("a dropdown the last scan lost is looked up again before it fails", () => {
  const control = (el: HTMLElement): RuntimeControl => ({ id: "f1", controlType: "combobox", el });

  it("a control still in the page is used as is, without a rescan", async () => {
    const el = document.createElement("input");
    document.body.append(el);
    let rescans = 0;
    const got = await liveControlFor("f1", () => control(el), async () => void rescans++);
    expect(got?.el).toBe(el);
    expect(rescans).toBe(0);
  });

  it("a control missing from the registry is found by the rescan (a scan taken while a menu hid it)", async () => {
    const el = document.createElement("input");
    document.body.append(el);
    const registry = new Map<string, RuntimeControl>();
    let rescans = 0;
    const got = await liveControlFor("f1", (id) => registry.get(id), async () => {
      rescans++;
      registry.set("f1", control(el));
    });
    expect(got?.el).toBe(el);
    expect(rescans).toBe(1);
  });

  it("a control whose element left the page is taken from the rescan, never written to detached", async () => {
    const gone = document.createElement("input");
    const fresh = document.createElement("input");
    document.body.append(fresh);
    const registry = new Map<string, RuntimeControl>([["f1", control(gone)]]);
    const got = await liveControlFor("f1", (id) => registry.get(id), async () => {
      registry.set("f1", control(fresh));
    });
    expect(got?.el).toBe(fresh);
  });

  it("a field the rescan cannot find either is reported missing, after one rescan", async () => {
    const gone = document.createElement("input");
    let rescans = 0;
    const got = await liveControlFor("f1", () => control(gone), async () => void rescans++);
    expect(got).toBeUndefined();
    expect(rescans).toBe(1);
  });

  it("a radio group is live while its first radio is in the page", async () => {
    const radio = document.createElement("input");
    radio.type = "radio";
    document.body.append(radio);
    let rescans = 0;
    const got = await liveControlFor("g1", () => ({ id: "g1", controlType: "radioGroup", radios: [radio] }), async () => void rescans++);
    expect(got?.radios?.[0]).toBe(radio);
    expect(rescans).toBe(0);
  });
});

describe("Workable's Address guessed from the visitor's location is the page's guess (Saalex, DISA)", () => {
  // Workable's public form JSON (GET /api/v1/jobs/<code>/form, Saalex, live
  // 2026-10-08) serves the Address it guessed from the requester's IP, and
  // the page puts that value in the box. The city here is made up.
  const FORM_JSON = [
    {
      name: "Personal information",
      fields: [
        { id: "firstname", required: true, label: "First name", type: "text" },
        {
          id: "address",
          required: true,
          label: "Address",
          helper: "Include your city, region, and country, so that employers can easily manage your application.",
          type: "text",
          value: "Springfield, United States",
          prefilledByLocation: true,
        },
        { id: "phone", required: false, label: "Phone", type: "phone", value: "" },
      ],
    },
  ];
  const WORKABLE_ADDRESS = (value: string) => `
    <form>
      <div class="styles--3IYUq styles--3JEd1"><label class="styles--3aPac"><span class="styles--1-9tY"><span><span class="styles--QTMDv styles--2TdGW" id="address_label"><strong class="styles--2kqW6">Address</strong></span></span></span>
        <div data-role="illustrated-input" class="styles--1tBNa"><div class="styles--3qHIU"><input aria-required="true" id="address" data-ui="address" name="address" aria-labelledby="address_label" aria-describedby="address_helper" required="" type="text" class="styles--2e9Cp" dir="auto" value="${value}"></div></div></label>
        <div class="styles--2v-7u"><div class="styles--1VNPc"><span class="styles--f-uLT" id="address_helper">Include your city, region, and country, so that employers can easily manage your application.</span></div></div>
      </div>
    </form>`;
  afterEach(() => setPageGuesses(new Map()));

  it("reads the guessed values from the form JSON, and only those", () => {
    expect([...prefilledGuesses(FORM_JSON).entries()]).toEqual([["address", "Springfield, United States"]]);
    expect(prefilledGuesses({ unexpected: true }).size).toBe(0);
  });

  it("finds the posting's code in Workable's application addresses", () => {
    expect(workableShortcode("https://apply.workable.com/saalex/j/2534F09970/apply/")).toBe("2534F09970");
    expect(workableShortcode("https://apply.workable.com/j/2534F09970/apply")).toBe("2534F09970");
    expect(workableShortcode("https://boards.greenhouse.io/acme/jobs/123")).toBeNull();
  });

  it("a box still holding the page's guess is empty to the fill, which writes the profile's location", () => {
    setPageGuesses(prefilledGuesses(FORM_JSON));
    document.body.innerHTML = WORKABLE_ADDRESS("Springfield, United States");
    const { fields } = scanPage(SPARSE_CANADIAN, false, null);
    const address = fields.find((f) => f.label === "Address");
    expect(address?.currentValue).toBeUndefined();
    expect(address?.proposedValue).toMatch(/Toronto/);
  });

  it("an Address the person typed is theirs, kept", () => {
    setPageGuesses(prefilledGuesses(FORM_JSON));
    document.body.innerHTML = WORKABLE_ADDRESS("Lyon, France");
    const { fields } = scanPage(SPARSE_CANADIAN, false, null);
    expect(fields.find((f) => f.label === "Address")?.currentValue).toBe("Lyon, France");
  });

  it("without the form JSON (another site, or the read failed) a filled box stays as it is", () => {
    document.body.innerHTML = WORKABLE_ADDRESS("Springfield, United States");
    const { fields } = scanPage(SPARSE_CANADIAN, false, null);
    expect(fields.find((f) => f.label === "Address")?.currentValue).toBe("Springfield, United States");
  });

  it("loads the guesses with one GET of the posting's form, and none when it fails", async () => {
    const asked: string[] = [];
    await loadWorkableGuesses("https://apply.workable.com/saalex/j/2534F09970/apply/", async (url: string) => {
      asked.push(url);
      return { ok: true, json: async () => FORM_JSON } as Response;
    });
    expect(asked).toEqual(["https://apply.workable.com/api/v1/jobs/2534F09970/form"]);
    document.body.innerHTML = WORKABLE_ADDRESS("Springfield, United States");
    expect(scanPage(SPARSE_CANADIAN, false, null).fields.find((f) => f.label === "Address")?.currentValue).toBeUndefined();

    setPageGuesses(new Map());
    await loadWorkableGuesses("https://apply.workable.com/saalex/j/2534F09970/apply/", async () => {
      throw new Error("offline");
    });
    expect(scanPage(SPARSE_CANADIAN, false, null).fields.find((f) => f.label === "Address")?.currentValue).toBe("Springfield, United States");
  });
});

describe("today's date in a signature box is written in the box's own format (Saalex on Workable)", () => {
  // Saalex's react-datepicker boxes show "DD/MM/YYYY" (the browser's en-GB
  // locale). Today went in month-first and the box read it day-first: on
  // 2026-10-08 the signature dates said 10 August (live; round 4's pins had
  // recorded "May" for 5 October the same way).
  const SIGNATURE_BOX = (placeholder: string) => `
    <form>
      <label for="sig"><strong>*</strong>Authorization Signature Date:</label>
      <div class="react-datepicker-wrapper"><div class="react-datepicker__input-container">
        <input id="sig" type="text" name="CA_31168" inputmode="tel" placeholder="${placeholder}" />
      </div></div>
    </form>`;
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(TEST_TODAY); // 2026-10-03
  });
  afterEach(() => vi.useRealTimers());

  const proposed = (placeholder: string) => {
    document.body.innerHTML = SIGNATURE_BOX(placeholder);
    return scanPage(SPARSE_CANADIAN, false, null).fields.find((f) => f.category === "signatureDate")?.proposedValue;
  };

  it("day first where the box shows DD/MM/YYYY", () => {
    expect(proposed("DD/MM/YYYY")).toBe("03/10/2026");
  });
  it("month first where it shows MM/DD/YYYY", () => {
    expect(proposed("MM/DD/YYYY")).toBe("10/03/2026");
  });
  it("year first where it shows YYYY-MM-DD", () => {
    expect(proposed("YYYY-MM-DD")).toBe("2026-10-03");
  });
});

describe("a radio the page re-renders when chosen still counts as chosen (Paylocity's How did you hear)", () => {
  // Choosing an option replaces that radio's element (Paylocity, live
  // 2026-10-08: the old one detached, a new one checked). The registry's
  // copy of the group lacked the chosen option, the check read it unchosen,
  // and the retry reported "No option matches" for an answer the page held.
  const option = (value: string) => `
    <label class="css-1hpmm7n" aria-labelledby="${value}-for-labelledby"><div class="css-k008qs"><div class="css-1v994a0">
      <input class="css-ingxr6" aria-checked="false" label="${value}" name="info.howDidYouHearAboutUs" type="radio" value="${value}">
    </div><div class="css-0" id="${value}-for-labelledby">${value}</div></div></label>`;
  const PAYLOCITY_HEAR = `
    <form>
      <label class="css-asocq5" for="info.howDidYouHearAboutUs"><span class="radio">How did you hear about us?</span>
        <div aria-required="true" role="radiogroup" data-automation-id="info.howDidYouHearAboutUs">
          ${["Online Job Board (LinkedIn, Indeed, etc.)", "Choice Website", "Friend or Family Member", "Other"].map(option).join("")}
        </div>
      </label>
    </form>`;
  const rerenderOnChoose = (e: Event) => {
    const r = e.target as HTMLInputElement;
    if (!(r instanceof HTMLInputElement) || r.type !== "radio") return;
    const wrap = r.parentElement as HTMLElement;
    queueMicrotask(() => {
      const fresh = wrap.cloneNode(true) as HTMLElement;
      const box = fresh.querySelector("input") as HTMLInputElement;
      box.checked = true;
      box.setAttribute("aria-checked", "true");
      wrap.replaceWith(fresh);
    });
  };
  afterEach(() => document.removeEventListener("click", rerenderOnChoose, true));

  it("the answer the page holds is reported filled, not 'No option matches'", async () => {
    document.body.innerHTML = PAYLOCITY_HEAR;
    document.addEventListener("click", rerenderOnChoose, true);
    const { fields, registry } = scanPage(SPARSE_CANADIAN, false, null);
    const hear = fields.find((f) => f.controlType === "radioGroup");
    expect(hear?.options).toContain("Choice Website");
    const engine = new AutofillReconciler({ sleep: async () => {}, observe: false });
    const [report] = await engine.run([{ fieldId: hear!.id, value: "Choice Website" }], registry);
    engine.dispose();
    expect((document.querySelector('input[value="Choice Website"]') as HTMLInputElement).checked).toBe(true);
    expect(report.reason).toBeUndefined();
    expect(report.ok).toBe(true);
  });
});

describe("Paylocity's own State select, as the live widget behaves (Choice Solutions, 2026-10-08)", () => {
  // Measured live: a click on the box only focuses its search input; ArrowDown
  // or typing opens the menu; the menu (the box's aria-owns) is a virtualized
  // list of role-less div.ListItemEven / div.ListItemOdd rows, eight rendered
  // at a time; it lists state CODES and filters them by prefix ("Tex" finds
  // "No Results Found"). Round 4's replica opened on click, had ARIA roles and
  // full names, and filled while the live page stayed on "Select a state".
  const CODES = ["AL", "AK", "AR", "AS", "AZ", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "TN", "TX", "UT"];
  function pctyLive(): HTMLElement {
    document.body.innerHTML = `
      <form><label class="css-asocq5" for="st"><span>State</span>
        <div class="pcty-input-select-full-container css-1zb4bw" id="st-wrap" aria-expanded="false" aria-haspopup="listbox" aria-owns="st-list" required="">
          <div class="pcty-input-select-input-container css-cw0yvh"><div class="css-6jkhxl" id="st-action"></div>
            <div class="css-n6sh4p"><div class="input-select-input-single-value css-1jybxfd">Select a state</div>
              <div class="pcty-input-select__input css-1je6tb3"><input aria-autocomplete="list" class="css-8nrzws" id="st" maxlength="250" type="text" value=""></div></div></div>
          <div class="css-1mj2ldx" aria-hidden="true"><div><span class="pcty-input-select__indicator-separator"></span><div class="pcty-input-select__dropdown-icon" aria-hidden="true" tabindex="0"></div></div></div>
        </div></label></form>`;
    const wrap = document.getElementById("st-wrap")!;
    const input = document.getElementById("st") as HTMLInputElement;
    const display = wrap.querySelector(".input-select-input-single-value")!;
    const render = (): void => {
      document.getElementById("st-list")?.remove();
      const q = input.value.trim().toLowerCase();
      const hits = CODES.filter((c) => c.toLowerCase().startsWith(q)).slice(0, 8);
      const box = document.createElement("div");
      box.id = "st-list";
      box.tabIndex = -1;
      box.innerHTML = `<div class="css-1ftq1i8 pcty-input-select__menu-list"><div style="height: 280px; width: 100%;"></div></div>`;
      const inner = box.querySelector("div > div") as HTMLElement;
      (hits.length ? hits : ["No Results Found"]).forEach((label, i) => {
        const row = document.createElement("div");
        row.className = `${i % 2 ? "ListItemOdd" : "ListItemEven"} css-1duv7c3`;
        row.textContent = label;
        if (hits.length) {
          row.addEventListener("click", () => {
            display.textContent = label;
            input.value = "";
            wrap.setAttribute("aria-expanded", "false");
            document.getElementById("st-list")?.remove();
          });
        }
        inner.append(row);
      });
      document.body.append(box);
      wrap.setAttribute("aria-expanded", "true");
    };
    wrap.addEventListener("click", () => input.focus());
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") render();
    });
    input.addEventListener("input", render);
    return wrap;
  }
  const fast = { sleep: async () => {}, openWaitMs: 200, commitWaitMs: 200, pollMs: 10 };

  it("a state named in full is found by its code and chosen", async () => {
    const wrap = pctyLive();
    const res = await fillAriaCombobox(wrap, "Texas", fast);
    expect(res).toMatchObject({ filled: true });
    expect(wrap.querySelector(".input-select-input-single-value")?.textContent).toBe("TX");
  });

  it("a state given as its code is chosen", async () => {
    const wrap = pctyLive();
    expect(await fillAriaCombobox(wrap, "TX", fast)).toMatchObject({ filled: true });
    expect(wrap.querySelector(".input-select-input-single-value")?.textContent).toBe("TX");
  });

  it("a place the list does not hold is a miss that leaves the box as it was", async () => {
    const wrap = pctyLive();
    const res = await fillAriaCombobox(wrap, "Ontario", fast);
    expect(res.filled).toBe(false);
    expect(wrap.querySelector(".input-select-input-single-value")?.textContent).toBe("Select a state");
    expect((document.getElementById("st") as HTMLInputElement).value).toBe("");
  });
});

describe("a state read back as its code is the state that was written (Paylocity's State)", () => {
  it("'Texas' showing as 'TX' is no revert, and another state still is", () => {
    expect(revertedFields([{ fieldId: "s", value: "Texas" }], [{ fieldId: "s", value: "TX" }], new Set(["s"]))).toEqual([]);
    expect(revertedFields([{ fieldId: "s", value: "TX" }], [{ fieldId: "s", value: "Texas" }], new Set(["s"]))).toEqual([]);
    expect(revertedFields([{ fieldId: "s", value: "Texas" }], [{ fieldId: "s", value: "TN" }], new Set(["s"]))).toEqual([{ fieldId: "s", cleared: false }]);
  });
});

describe("Dayforce's preferred contact method and dial codes (Eclipse, live 2026-10-05 and 10-08)", () => {
  const askChoice = (label: string, options: string[]) => {
    const q: QuestionInput = { label, controlType: "select", options, category: "unknown", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(SPARSE_CANADIAN, TEST_TODAY), SPARSE_CANADIAN, { jobCountry: "CA", company: "Eclipse" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };

  it("'Preferred Contact Method' is the email the application gives, when the list offers it", () => {
    expect(askChoice("Preferred Contact Method", ["Email", "Mobile Phone", "Home Phone"])).toBe("Email");
    expect(askChoice("How would you prefer we contact you?", ["Phone call", "Text message", "E-mail"])).toBe("E-mail");
  });

  it("a list without email is left to the applicant", () => {
    expect(askChoice("Preferred Contact Method", ["Mobile Phone", "Home Phone"])).not.toBe("Mobile Phone");
    expect(askChoice("Preferred Contact Method", ["Mobile Phone", "Home Phone"])).not.toBe("Home Phone");
  });

  it("a dial-code picker showing the flag and code of the country written is no revert", () => {
    expect(revertedFields([{ fieldId: "d", value: "Canada" }], [{ fieldId: "d", value: "🇨🇦 +1" }], new Set(["d"]))).toEqual([]);
    // The flag decides: +1 is not enough when the flag is another country's.
    expect(revertedFields([{ fieldId: "d", value: "United States" }], [{ fieldId: "d", value: "🇨🇦 +1" }], new Set(["d"]))).toEqual([{ fieldId: "d", cleared: false }]);
  });
});

describe("a start date between two of the list's ranges takes the later one (Striveworks, question bank)", () => {
  // "What is your earliest available start date…?" with ranges from offer
  // acceptance. Available in 11 days fell between "Immediately" (a week) and
  // "2 to 4 weeks" and was left blank (bank re-run 2026-10-08). The later
  // range is still true; an earlier one would promise a start they cannot make.
  const STRIVEWORKS = ["Immediately", "2 to 4 weeks from offer acceptance", "4-8 weeks from offer acceptance", "8-12 weeks from offer acceptance", "12+ weeks from offer acceptance"];
  const startIn = (isoDate: string) => {
    const profile = { ...SPARSE_CANADIAN, earliestStartDate: isoDate };
    const q: QuestionInput = { label: "What is your earliest available start date for full-time employment in Austin, Texas?", controlType: "select", options: STRIVEWORKS, category: "unknown", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(profile, TEST_TODAY), profile, { jobCountry: "US", company: "Striveworks" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };

  it("11 days away: '2 to 4 weeks', never 'Immediately'", () => {
    expect(startIn("2026-10-14")).toBe("2 to 4 weeks from offer acceptance");
  });
  it("a range that holds the days still wins", () => {
    expect(startIn("2026-10-03")).toBe("Immediately");
    expect(startIn("2026-10-24")).toBe("2 to 4 weeks from offer acceptance");
    expect(startIn("2027-03-01")).toBe("12+ weeks from offer acceptance");
  });
});

describe("lists that tell living there from moving there (Ashby question bank, 2026-10-08)", () => {
  // Iambic, Pryzm, Zip, Vital Lyfe (Ashby bank): the first "Yes" was taken for
  // anyone willing to move, so people in Toronto, Berlin and Bengaluru said
  // "Yes, I live in San Diego" and "In Boston".
  const ask = (persona: UserApplicationProfile, label: string, options: string[], ctx: { jobCountry: string; jobCity: string; company: string }) => {
    const q: QuestionInput = { label, controlType: "select", options, category: "unknown", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, ctx);
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  const SD = ["Yes, I live in San Diego", "I do not live in San Diego but I am willing to relocate", "No, I do not live in San Diego and am not willing to relocate"];
  const iambic = { jobCountry: "US", jobCity: "San Diego", company: "Iambic Therapeutics" };
  it("Iambic: someone elsewhere who will move says so; someone who will not, says that", () => {
    expect(ask(P.COMPLETE_CANADIAN, "Do you live in San Diego or are you willing to relocate?", SD, iambic)).toBe(SD[1]);
    expect(ask(P.BERLIN_STAFF, "Do you live in San Diego or are you willing to relocate?", SD, iambic)).toBe(SD[1]);
    expect(ask(P.US_H1B_SENIOR, "Do you live in San Diego or are you willing to relocate?", SD, iambic)).toBe(SD[2]);
  });
  const pryzm = { jobCountry: "US", jobCity: "Boston", company: "Belisar" };
  const BOS = "This is an on-site position in our Boston office. Are you located in Boston or willing to relocate?";
  it("Pryzm: 'In Boston' only for someone in Boston", () => {
    expect(ask(P.US_OPT_ANALYST, BOS, ["In Boston", "Planning to Relocate"], pryzm)).toBe("In Boston");
    expect(ask(P.COMPLETE_CANADIAN, BOS, ["In Boston", "Planning to Relocate"], pryzm)).toBe("Planning to Relocate");
    expect(ask(P.US_H1B_SENIOR, BOS, ["In Boston", "Planning to Relocate"], pryzm)).not.toBe("In Boston");
  });
  const ZIP = ["Yes and I am local to the San Francisco Bay Area", "Yes but I would need to relocate", "No I am not willing to come into the office"];
  const zip = { jobCountry: "US", jobCity: "San Francisco", company: "Zip" };
  it("Zip: a willing mover 'would need to relocate'; a San Jose resident is not decided for them", () => {
    expect(ask(P.US_OPT_ANALYST, "Are you willing and able to come into our downtown SF office 3 days per week?", ZIP, zip)).toBe(ZIP[1]);
    expect(ask(P.INDIA_NEW_GRAD, "Are you willing and able to come into our downtown SF office 3 days per week?", ZIP, zip)).toBe(ZIP[1]);
    expect(ask(P.US_GREENCARD_STUDENT, "Are you willing and able to come into our downtown SF office 3 days per week?", ZIP, zip)).not.toBe(ZIP[1]);
  });
  it("Vital Lyfe: a plain Yes beside 'Yes, but require relocation' means no move", () => {
    const opts = ["Yes", "No", "Yes, but require relocation"];
    const vl = { jobCountry: "US", jobCity: "Los Angeles", company: "Vital Lyfe" };
    expect(ask(P.COMPLETE_CANADIAN, "Are you open to working out of our Torrance (LA) Office 5 Days a Week?", opts, vl)).toBe("Yes, but require relocation");
    expect(ask(P.BOOTCAMP_CAREER_GAP, "Are you open to working out of our Torrance (LA) Office 5 Days a Week?", opts, vl)).toBe("No");
  });
  it("Teleskope: 'currently based, or planning to be based in NYC' is Yes for someone moving there", () => {
    const tk = { jobCountry: "US", jobCity: "New York", company: "Teleskope" };
    const q = "Are you currently based, or planning to be based in NYC and able to work on-site (hybrid) in the Financial District?";
    expect(ask(P.COMPLETE_CANADIAN, q, ["Yes", "No"], tk)).toBe("Yes");
    expect(ask(P.BERLIN_STAFF, q, ["Yes", "No"], tk)).toBe("Yes");
    expect(ask(P.US_H1B_SENIOR, q, ["Yes", "No"], tk)).toBe("No");
  });
});

describe("living there, read across every place and time zone a question names (bank re-run, 2026-10-08)", () => {
  const ask = (persona: UserApplicationProfile, label: string, options: string[], ctx: { jobCountry: string; jobCity: string; company: string }) => {
    const q: QuestionInput = { label, controlType: "select", options, category: "unknown", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, ctx);
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  const MTL_Q = "This position is required to work out of a Lyft Office in Montreal, if you do not reside within the country and within commutable proximity to the office, are you open to relocating?";
  const MTL = ["I am willing to relocate before starting employment.", "I am not willing to relocate before starting employment.", "I already reside within commutable distance to Montreal and am able to work at an On-site Office."];
  const lyft = { jobCountry: "CA", jobCity: "Montreal", company: "Lyft" };
  it("Lyft: Toronto is no commute to Montreal, though Ontario borders Quebec", () => {
    expect(ask(P.COMPLETE_CANADIAN, MTL_Q, MTL, lyft)).toBe(MTL[0]);
    expect(ask(P.MONTREAL_CHANGER, MTL_Q, MTL, lyft)).toBe(MTL[2]);
  });
  const JERSEY = { ...P.US_H1B_SENIOR, location: "Jersey City, NJ", addressStreet: "", addressCity: "Jersey City", addressState: "NJ", postalCode: "07302" };
  it("two places named: No only when the applicant is at neither; a neighbouring state is not decided", () => {
    const mx = { jobCountry: "US", jobCity: "San Francisco", company: "Mixpanel" };
    const q = "Are you currently located in the San Francisco Bay Area or New York City?";
    expect(ask(P.US_OPT_ANALYST, q, ["Yes", "No"], mx)).toBe("No");
    expect(ask(JERSEY, q, ["Yes", "No"], mx)).not.toBe("No");
  });
  it("a time zone named beside the place counts: Seattle is in the Pacific time zone", () => {
    const amp = { jobCountry: "US", jobCity: "San Francisco", company: "Amplitude" };
    const q = "Are you currently based in the San Francisco Bay Area or within the Pacific time zone?";
    expect(ask(P.US_H1B_SENIOR, q, ["Yes", "No"], amp)).toBe("Yes");
    expect(ask(P.BOOTCAMP_CAREER_GAP, q, ["Yes", "No"], amp)).toBe("No");
  });
  it("an answer that is one checkbox's whole text is that box alone (Vital Lyfe's checkboxes)", () => {
    document.body.innerHTML = `<form><fieldset class="field"><legend>Are you open to working out of our Torrance (LA) Office 5 Days a Week?</legend>
      <label><input type="checkbox" name="q[]" value="0"> Yes</label><label><input type="checkbox" name="q[]" value="1"> No</label>
      <label><input type="checkbox" name="q[]" value="2"> Yes, but require relocation</label></fieldset></form>`;
    setResolveContext({ jobCountry: "US", jobCity: "Los Angeles", company: "Vital Lyfe" });
    const f = scanPage(P.COMPLETE_CANADIAN as UserApplicationProfile, false, null).fields.find((x) => x.controlType === "checkboxGroup");
    expect(f?.proposedValue).toBe("Yes, but require relocation");
    setResolveContext({ jobCountry: null, jobCity: null, company: "" });
  });
});

describe("work-right questions read by what they ask (Ashby question bank, 2026-10-08)", () => {
  const ask = (persona: UserApplicationProfile, label: string, options: string[], jobCountry = "US") => {
    const q: QuestionInput = { label, controlType: "select", options, category: "unknown", kind: options.length === 2 && options[0] === "Yes" ? "boolean" : "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry, company: "Acme" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  it("'Will you now or in the future require authorization to work in the US?' is a need, not a right (TensorWave)", () => {
    const q = "Will you now or in the future require authorization to work in the United States?";
    expect(ask(P.COMPLETE_CANADIAN, q, ["Yes", "No"])).toBe("Yes");
    expect(ask(P.BOOTCAMP_CAREER_GAP, q, ["Yes", "No"])).toBe("No");
    expect(ask(P.US_OPT_ANALYST, q, ["Yes", "No"])).toBe("Yes");
    expect(ask(P.US_H1B_SENIOR, q, ["Yes", "No"])).toBe("Yes");
    expect(ask(P.INDIA_NEW_GRAD, q, ["Yes", "No"])).toBe("Yes");
  });
  const TRULIOO = ["Yes, I currently require sponsorship", "No, I do not currently require sponsorship, but I will require sponsorship in the future.", "No, I do not require sponsorship now or in the future."];
  it("Trulioo's three sponsorship options: now, only later, never", () => {
    const q = "Will you now or in the future require Trulioo to sponsor your employment authorization to work in this location?";
    expect(ask(P.US_H1B_SENIOR, q, TRULIOO)).toBe(TRULIOO[0]);
    expect(ask(P.US_OPT_ANALYST, q, TRULIOO)).toBe(TRULIOO[1]);
    expect(ask(P.BOOTCAMP_CAREER_GAP, q, TRULIOO)).toBe(TRULIOO[2]);
  });
  const ELIGIBLE = ["Yes - I am legally eligible to work in Canada", "Yes - I am legally eligible to work in the USA", "No - I am not legally eligible to work in Canada or the USA"];
  it("'eligible to work in Canada or the USA?' takes the country the applicant may work in", () => {
    const q = "Are you legally eligible to work in Canada or the USA?";
    expect(ask(P.COMPLETE_CANADIAN, q, ELIGIBLE)).toBe(ELIGIBLE[0]);
    expect(ask(P.BOOTCAMP_CAREER_GAP, q, ELIGIBLE)).toBe(ELIGIBLE[1]);
    expect(ask(P.BERLIN_STAFF, q, ELIGIBLE)).toBe(ELIGIBLE[2]);
  });
  it("'prevented from lawfully becoming employed in the US' is the inverse of the right, never 'needs sponsorship' (Barnes & Thornburg)", () => {
    const q = "Are you prevented from lawfully becoming employed in the US because of visa or immigration status? (Proof of citizenship or immigration status will be required upon employment)";
    expect(ask(P.COMPLETE_CANADIAN, q, ["Yes", "No"])).toBe("Yes");
    expect(ask(P.BOOTCAMP_CAREER_GAP, q, ["Yes", "No"])).toBe("No");
    // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
    expect(ask(P.US_OPT_ANALYST, q, ["Yes", "No"])).toBe("abstain");
    expect(ask(P.US_H1B_SENIOR, q, ["Yes", "No"])).not.toBe("No");
  });
});

describe("a school asked at one level of study (Barnes & Thornburg on Ashby, question bank 2026-10-08)", () => {
  // "Law School", "Undergraduate School", "Graduate School" and their majors
  // all got the profile's first school: a bootcamp as an undergraduate
  // school, a bachelor's as a graduate school, everyone's university as a
  // law school, and "Law School Graduation Year" a whole education summary.
  const ask = (persona: UserApplicationProfile, label: string, category: "school" | "fieldOfStudy" | "graduationYear" = "school") => {
    const q: QuestionInput = { label, controlType: "text", category, kind: "text" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "US", company: "Barnes & Thornburg" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  it("Undergraduate School is the bachelor's school, never a bootcamp or a master's", () => {
    expect(ask(P.US_H1B_SENIOR, "Undergraduate School")).toBe("Hanoi University of Science and Technology");
    expect(ask(P.COMPLETE_CANADIAN, "Undergraduate School")).toBe("University of Waterloo");
    expect(ask(P.BOOTCAMP_CAREER_GAP, "Undergraduate School")).toBe("abstain");
  });
  it("Graduate School and Graduate Major only for a graduate degree", () => {
    expect(ask(P.US_H1B_SENIOR, "Graduate School")).toBe("University of Washington");
    expect(ask(P.INDIA_NEW_GRAD, "Graduate School")).toBe("abstain");
    expect(ask(P.COMPLETE_CANADIAN, "Graduate Major", "fieldOfStudy")).toBe("abstain");
    expect(ask(P.US_H1B_SENIOR, "Graduate Major", "fieldOfStudy")).toBe("Information Systems");
  });
  it("Law School and its year only for a law degree", () => {
    expect(ask(P.US_H1B_SENIOR, "Law School")).toBe("abstain");
    expect(ask(P.COMPLETE_CANADIAN, "Law School Graduation Year", "graduationYear")).toBe("abstain");
    const lawyer = { ...P.US_H1B_SENIOR, education: [{ school: "Georgetown University Law Center", degree: "Juris Doctor (J.D.)", graduationYear: "2019" }, ...P.US_H1B_SENIOR.education] };
    expect(ask(lawyer, "Law School")).toBe("Georgetown University Law Center");
    expect(ask(lawyer, "Law School Graduation Year", "graduationYear")).toBe("2019");
  });
});

describe("Replit's Foster City pair (Ashby question bank, 2026-10-08)", () => {
  const ask = (persona: UserApplicationProfile, label: string) => {
    const q: QuestionInput = { label, controlType: "select", options: ["Yes", "No"], category: "unknown", kind: "boolean" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "US", jobCity: "Foster City", company: "Replit" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  it("'work from our Foster City, CA HQ 3 days per week?' is an in-office question: No for someone elsewhere who will not move", () => {
    const q = "Are you able to work from our Foster City, CA HQ 3 days per week?";
    expect(ask(P.US_H1B_SENIOR, q)).toBe("No");
    expect(ask(P.BOOTCAMP_CAREER_GAP, q)).toBe("No");
    expect(ask(P.COMPLETE_CANADIAN, q)).toBe("Yes");
  });
  it("'If not currently in the Bay Area, are you willing to relocate…?' is decided by where the applicant lives", () => {
    const q = "If not currently in the Bay Area, are you willing to relocate near our Foster City, CA Office?";
    expect(ask(P.COMPLETE_CANADIAN, q)).toBe("Yes");
    expect(ask(P.US_H1B_SENIOR, q)).toBe("No");
    expect(ask(P.US_GREENCARD_STUDENT, q)).not.toBe("Yes");
  });
});

describe("a graduation month never matches another month's option (Perchwell, Ashby question bank 2026-10-08)", () => {
  const PERCHWELL = ["December 2026", "Spring 2027", "December 2027", "Spring 2028", "December 2028", "Other"];
  const grad = (persona: UserApplicationProfile) => {
    const q: QuestionInput = { label: "What is your expected graduation year?", controlType: "select", options: PERCHWELL, category: "graduationYear", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "US", company: "Perchwell" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  it("a June 2026 graduate is not 'December 2026'", () => {
    expect(grad(P.INDIA_NEW_GRAD)).not.toBe("December 2026");
  });
  it("an April 2027 graduate is still 'Spring 2027'", () => {
    expect(grad(P.COMPLETE_CANADIAN)).toBe("Spring 2027");
  });
});

describe("a notice-period list takes the notice stated (Exegy, Ashby question bank 2026-10-08)", () => {
  const EXEGY = ["Available Immediately", "2 - 4 weeks", "4 - 8 weeks", "8 weeks +"];
  const notice = (persona: UserApplicationProfile) => {
    const q: QuestionInput = { label: "What is your notice period to begin working with Exegy?", controlType: "select", options: EXEGY, category: "noticePeriod", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "CA", company: "Exegy" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  it("two weeks, immediately, three months: by the notice, not by a start date months away", () => {
    expect(notice(P.COMPLETE_CANADIAN)).toBe("2 - 4 weeks");
    expect(notice(P.BOOTCAMP_CAREER_GAP)).toBe("Available Immediately");
    expect(notice(P.INDIA_NEW_GRAD)).toBe("Available Immediately");
    expect(notice(P.BERLIN_STAFF)).toBe("8 weeks +");
  });
});

describe("a bare salary figure is in the applicant's own currency (Float, Ashby question bank 2026-10-08)", () => {
  const salary = (persona: UserApplicationProfile, label: string) => {
    const q: QuestionInput = { label, controlType: "text", category: "salary", kind: "number" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "CA", company: "Float" });
    return r?.status ?? null;
  };
  it("a Denver resident's '85000' never goes into a box asking for CAD; a Toronto resident's does", () => {
    const q = "What annual salary (in CAD) would make you excited to sign an offer for this role?";
    expect(salary(P.BOOTCAMP_CAREER_GAP, q)).toBe("abstain");
    expect(salary(P.COMPLETE_CANADIAN, q)).not.toBe("abstain");
  });
});

describe("the company's own site named with a connector (Miter, Ashby question bank 2026-10-08)", () => {
  it("'Miter website or careers page' is Miter's own site for someone who found it there", () => {
    const MITER = ["Career fair ", "Campus event or club", "Miter employee referral", "LinkedIn/job board", "Miter website or careers page", "Social media", "Recruiter outreach", "Other"];
    const q: QuestionInput = { label: "How did you hear about this role?", controlType: "select", options: MITER, category: "unknown", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(P.US_H1B_SENIOR, TEST_TODAY), P.US_H1B_SENIOR, { jobCountry: "US", company: "Miter" });
    expect(r && r.status === "answer" ? r.value : r?.status).toBe("Miter website or careers page");
  });
});

describe("U.S. person questions in their other wordings (Ashby question bank, 2026-10-08)", () => {
  const ask = (persona: UserApplicationProfile, label: string, options = ["Yes", "No"]) => {
    const q: QuestionInput = { label, controlType: "select", options, category: "unknown", kind: options.length === 2 ? "boolean" : "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "US", company: "Acme" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  const SARONIC = "The individual performing this role will need to access material that legally requires “U.S. Person” status. A “U.S. Person” must meet ONE of the following three criteria: 1) A U.S. citizen, 2) A legal permanent resident (also known as a green card holder), OR 3) A refugee or successful asylee (someone who has completed the asylum process and formally received a grant of asylum). To ensure eligibility for this role, please confirm whether you fall into one of the three statuses above.";
  const GECKO = "This role requires access to export-controlled technical data, U.S. Government systems, and customer facilities subject to U.S.-person access requirements. For purposes of this role, a “U.S. person” is a U.S. citizen or national, lawful permanent resident, refugee, asylee, or another protected individual as defined by 8 U.S.C. § 1324b(a)(3). Do you currently qualify as a U.S. person under this definition?";
  const REFLECT = "Do any of the following designations apply to you: U.S. citizen or national, U.S. lawful permanent resident (aka green card holder), Refugee under 8 U.S.C. § 1157, or Asylee under 8 U.S.C. § 1158?";
  const COWBOY = "As defined in the ITAR, “U.S. Persons” include U.S. citizens, lawful permanent residents (i.e., Green Card holders), and certain protected individuals (e.g., refugees/asylees, American Samoans). Please consult with a knowledgeable advisor if you are unsure whether you are a “U.S. Person.”";
  it("a citizen is one; a Canadian, a German or a visa holder is not", () => {
    for (const label of [SARONIC, GECKO, REFLECT, COWBOY]) {
      expect(ask(P.BOOTCAMP_CAREER_GAP, label), label.slice(0, 40)).toBe("Yes");
      expect(ask(P.COMPLETE_CANADIAN, label), label.slice(0, 40)).toBe("No");
      // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
      expect(ask(P.US_H1B_SENIOR, label), label.slice(0, 40)).toBe("abstain");
    }
    expect(ask(P.BERLIN_STAFF, GECKO, ["Yes", "No", "Unsure \u2014 I would like Gecko Robotics to review my eligibility"])).toBe("No");
  });
  it("Snowflake's status list: the U.S. person, or 'a citizen of a different country' when the citizenship is known and not embargoed", () => {
    const SNOW = ["I am a U.S. person", "I am a citizen of Cuba, Iran, North Korea, or Syria AND I am NOT a U.S. person", "None of the above; I am a citizen of a different country"];
    const q = "A “U.S. person” is a citizen, legal permanent resident, or legal temporary resident (i.e., a refugee or asylee) of the United States. Which of the following best describes your “U.S. person” status?";
    expect(ask(P.BOOTCAMP_CAREER_GAP, q, SNOW)).toBe(SNOW[0]);
    expect(ask(P.COMPLETE_CANADIAN, q, SNOW)).toBe(SNOW[2]);
    expect(ask(P.INDIA_NEW_GRAD, q, SNOW)).toBe(SNOW[2]);
  });
});

describe("blanks the Ashby question bank's profiles answer (2026-10-08)", () => {
  const ask = (persona: UserApplicationProfile, label: string, options: string[] = [], category = "unknown", kind?: "boolean" | "choice" | "text") => {
    const q: QuestionInput = { label, controlType: options.length ? "select" : "text", options, category: category as QuestionInput["category"], kind: kind ?? (options.length === 2 && options[0] === "Yes" ? "boolean" : options.length ? "choice" : "text") };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "US", company: "Iambic Therapeutics" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  it("'Where did you first see this position?' and 'How'd you hear about Range?' are the how-did-you-hear question", () => {
    const IAMBIC = ["Iambic Website", "Indeed", "LinkedIn Job Listing", "LinkedIn Post", "Referral", "ZipRecruiter", "Other"];
    expect(ask(P.COMPLETE_CANADIAN, "Where did you first see this position?", IAMBIC)).toBe("LinkedIn Job Listing");
    expect(ask(P.US_H1B_SENIOR, "Where did you first see this position?", IAMBIC)).toBe("Iambic Website");
    expect(ask(P.COMPLETE_CANADIAN, "How'd you hear about Range?", ["LinkedIn", "BuiltIn", "Network", "Google search", "Next Play", "Other"])).toBe("LinkedIn");
  });
  it("'Have you ever filed an application with us before?' is a prior application: No", () => {
    expect(ask(P.COMPLETE_CANADIAN, "Have you ever  filed an application with us before?", ["Yes", "No"])).toBe("No");
  });
  it("'require sponorship' (sic) to work in the country you reside in is the sponsorship question", () => {
    const q = "Do you, now or in the future, require sponorship to work in the country you currently reside in?";
    expect(ask(P.COMPLETE_CANADIAN, q, ["Yes", "No"])).toBe("No");
    expect(ask(P.US_H1B_SENIOR, q, ["Yes", "No"])).toBe("Yes");
    expect(ask(P.BOOTCAMP_CAREER_GAP, q, ["Yes", "No"])).toBe("No");
  });
  it("'Current or most recent employer' takes a job that has ended", () => {
    expect(ask(P.BOOTCAMP_CAREER_GAP, "Current or most recent employer", [], "currentCompany")).toBe("Ibotta");
    expect(ask(P.BOOTCAMP_CAREER_GAP, "Current/Last Company", [], "currentCompany")).toBe("Ibotta");
    expect(ask(P.BOOTCAMP_CAREER_GAP, "Where have you most recently worked?")).toBe("Ibotta");
    expect(ask(P.COMPLETE_CANADIAN, "Current or most recent employer", [], "currentCompany")).toBe("Kinaxis");
  });
  it("visa status lists take the applicant's own visa", () => {
    const BASE = ["I am a U.S. citizen or permanent resident (Green Card holder)", "I currently hold an H-1B visa and would need a transfer to Base", "I hold a TN visa and can work for Base with a new employer petition, or I am a Canadian Citizen eligible for a TN1A visa under the USCMA", "I am an Australian citizen and eligible for an E-3 visa", "I currently hold OPT and will require H-1B sponsorship in the future", "I currently hold STEM OPT and will require H-1B sponsorship in the future", "I am authorized to work in the U.S. under CPT (Curricular Practical Training) for the duration of my academic program", "I require initial H-1B sponsorship to work for Base now", "I require another type of visa sponsorship to work for Base now", "I am not currently authorized to work in the U.S."];
    expect(ask(P.US_H1B_SENIOR, "Are you legally authorized to work in the U.S.?", BASE)).toBe(BASE[1]);
    expect(ask(P.US_OPT_ANALYST, "Are you legally authorized to work in the U.S.?", BASE)).toBe(BASE[5]);
    const VL = ["US Citizen", "US Permanent Resident", "Canadian or Mexican Citizen (TN Visa)", "STEM OPT", "Non-US Person with work Authorization w/ H1B, H-4", "O-1 Visa - Non-US Person with work Authorization"];
    expect(ask(P.US_OPT_ANALYST, "What's your citizenship / employment eligibility?", VL)).toBe("STEM OPT");
    expect(ask(P.US_H1B_SENIOR, "What's your citizenship / employment eligibility?", VL)).toBe(VL[4]);
    expect(ask(P.COMPLETE_CANADIAN, "What's your citizenship / employment eligibility?", VL)).toBe(VL[2]);
    const ONE = ["F-1 Student (OPT)", "F-1 Student (STEM OPT)", "F-1 Student (CPT)", "H-1B", "H-1B (Transfer)", "TN", "L-1", "E-3", "Asylee/Refugee", "Adjustment of Status Pending"];
    const q = "If you answered “Yes” above, please select your current immigration status from the dropdown below.";
    expect(ask(P.US_H1B_SENIOR, q, ONE)).toBe("H-1B (Transfer)");
    expect(ask(P.US_OPT_ANALYST, q, ONE)).toBe("F-1 Student (STEM OPT)");
    expect(ask(P.BOOTCAMP_CAREER_GAP, q, ONE)).not.toBe("H-1B");
  });
  it("someone enrolled nowhere is not enrolled at a named place (Superhuman, regression 2026-10-08)", () => {
    // Left to the AI (down since 08-25): blank for a graduate.
    const q = "Are you currently attending a college or university in the US?";
    expect(ask(P.MONTREAL_CHANGER, q, ["Yes", "No"])).toBe("No");
    expect(ask(P.US_H1B_SENIOR, "Are you currently enrolled at a Canadian institution?", ["Yes", "No"])).toBe("No");
    // A student's school is somewhere the profile does not say: theirs.
    expect(["Yes", "No"]).not.toContain(ask(P.COMPLETE_CANADIAN, q, ["Yes", "No"]));
    // A time named is another question.
    expect(["Yes", "No"]).not.toContain(ask(P.MONTREAL_CHANGER, "Will you be currently enrolled in school during the internship?", ["Yes", "No"]));
  });
  it("a visa open to a country's citizens is not the citizen's own status (Waymo, regression 2026-10-08)", () => {
    // Waymo asks which authorization the applicant needs; TN is one a
    // Canadian MAY use (with a degree), not one anybody stated. Round 4 left
    // it blank; the citizenship reading picked TN for a Canadian student.
    const WAYMO = ["CPT", "OPT", "TN (Applicable for citizens of Canada or Mexico)", "J-1", "Other", "I don't know", "Not applicable"];
    const kind = "If yes, what kind? (If you do not require work authorization sponsorship please select Not Applicable.)";
    expect(WAYMO).not.toContain(ask(P.COMPLETE_CANADIAN, kind, WAYMO));
    expect(ask(P.US_OPT_ANALYST, kind, WAYMO)).toBe("OPT");
    // An option that names the applicant by citizenship still stands.
    const VL = ["US Citizen", "US Permanent Resident", "Canadian or Mexican Citizen (TN Visa)", "STEM OPT", "Non-US Person with work Authorization w/ H1B, H-4"];
    expect(ask(P.COMPLETE_CANADIAN, "What's your citizenship / employment eligibility?", VL)).toBe(VL[2]);
  });
});

describe("Ashby's Yes/No buttons are a question (every Ashby Boolean field, live 2026-10-08)", () => {
  // An Ashby Boolean question is two buttons with aria-pressed and a hidden
  // checkbox; its <label for> names the checkbox's NAME, so the checkbox read
  // "No" as its label and no work-right or sponsorship question on Ashby was
  // ever answered (TensorWave, Teleskope, Saronic, Replit, Amplitude live).
  const ENTRY = (q: string, name: string) => `
    <div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry" data-field-path="${name}">
      <label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 ashby-application-form-question-title" for="${name}">${q}</label>
      <div class="_container_1svni_28 _yesno_1e3gg_148 ashby-application-form-input-yesno">
        <button class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="yes">Yes</button>
        <button class="_container_pjyt6_1 _option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="no">No</button>
        <input type="checkbox" class="_input_1svni_78" tabindex="-1" name="${name}">
      </div>
    </div>`;
  // The page's own handler: a button with no type inside the form would
  // submit it, so Ashby prevents that and flips aria-pressed.
  const press = (e: Event) => {
    const b = (e.target as HTMLElement).closest("button[aria-pressed]");
    if (!b) return;
    e.preventDefault();
    for (const other of Array.from(b.parentElement!.querySelectorAll("button[aria-pressed]"))) other.setAttribute("aria-pressed", String(other === b));
  };
  beforeEach(() => document.addEventListener("click", press, true));
  afterEach(() => document.removeEventListener("click", press, true));
  const Q = "Will you now or in the future require authorization to work in the United States?";

  it("is scanned as one Yes/No question under its own label, never a checkbox called 'No'", () => {
    document.body.innerHTML = `<form>${ENTRY(Q, "c123fdc7-d598-4076-add5-2ab50fe1e64e")}</form>`;
    setResolveContext({ jobCountry: "US", jobCity: "Las Vegas", company: "TensorWave" });
    const { fields } = scanPage(P.BOOTCAMP_CAREER_GAP as UserApplicationProfile, false, null);
    setResolveContext({ jobCountry: null, jobCity: null, company: "" });
    expect(fields).toHaveLength(1);
    expect(fields[0].controlType).toBe("ariaRadioGroup");
    expect(fields[0].label).toBe(Q);
    expect(fields[0].options).toEqual(["Yes", "No"]);
    expect(fields[0].proposedValue).toBe("No");
  });

  it("is answered by pressing the button, checked by aria-pressed, and empty until then", async () => {
    document.body.innerHTML = `<form>${ENTRY(Q, "c123fdc7-d598-4076-add5-2ab50fe1e64e")}</form>`;
    const { fields, registry } = scanPage(P.BOOTCAMP_CAREER_GAP as UserApplicationProfile, false, null);
    const control = registry.get(fields[0].id)!;
    expect(holdsNoAnswer(control, () => undefined)).toBe(true);
    const engine = new AutofillReconciler({ sleep: async () => {}, observe: false });
    const [report] = await engine.run([{ fieldId: fields[0].id, value: "No" }], registry);
    engine.dispose();
    expect(document.querySelector('button[data-option="no"]')?.getAttribute("aria-pressed")).toBe("true");
    expect(report.ok).toBe(true);
    expect(holdsNoAnswer(control, () => undefined)).toBe(false);
  });
});

describe("a form's later sections are part of it (Ashby TensorWave: both Yes/No questions cut off)", () => {
  // TensorWave's form is one container of two sections. Every field the
  // scanner recognizes (name, email, phone, links) is in the first, so the
  // scope their common ancestor drew was that section, and the second one,
  // with both Yes/No questions and the consent radio, was dropped as outside
  // the form (live 2026-10-08: the panel listed 9 fields, the page has 12).
  const ENTRY = (q: string, inner: string) =>
    `<div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry"><label class="_heading_f7cvd_52 _label_1e3gg_42 ashby-application-form-question-title" for="${q.length}">${q}</label>${inner}</div>`;
  const TEXT = (q: string, id: string) => ENTRY(q, `<input id="${id}" type="text" class="_input_80epu_28 ashby-application-form-input-text" name="${id}">`).replace(`for="${q.length}"`, `for="${id}"`);
  const YESNO = (q: string, name: string) =>
    ENTRY(q, `<div class="_container_1svni_28 _yesno_1e3gg_148 ashby-application-form-input-yesno"><button class="_option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="yes">Yes</button><button class="_option_1svni_32 ashby-application-form-input-yesno-option" aria-pressed="false" data-option="no">No</button><input type="checkbox" tabindex="-1" name="${name}"></div>`).replace(`for="${q.length}"`, `for="${name}"`);
  const PAGE = `
    <div class="ashby-job-posting-right-pane">
      <div class="_jobPostingForm_5yu8i_402 ashby-application-form-container">
        <div class="_section_5yu8i_86 ashby-application-form-section-container">
          ${TEXT("Name", "_systemfield_name")}${TEXT("Email", "_systemfield_email")}${TEXT("LinkedIn", "li")}${TEXT("GitHub", "gh")}
        </div>
        <div class="_section_5yu8i_86 ashby-application-form-section-container">
          ${YESNO("Will you now or in the future require authorization to work in the United States?", "c123")}
          ${YESNO("Are you willing to work on-site in our Las Vegas office 5 days a week?", "099d")}
        </div>
      </div>
      <div class="_sidebar_x9 job-alerts"><label for="kw">Keywords</label><input id="kw" type="text"></div>
    </div>`;

  it("keeps the questions of a sibling section of the same kind, and still drops a widget beside the form", () => {
    document.body.innerHTML = PAGE;
    setResolveContext({ jobCountry: "US", jobCity: "Las Vegas", company: "TensorWave" });
    const { fields } = scanPage(P.BOOTCAMP_CAREER_GAP as UserApplicationProfile, false, null);
    setResolveContext({ jobCountry: null, jobCity: null, company: "" });
    const byLabel = new Map(fields.map((f) => [f.label, f.proposedValue]));
    expect(byLabel.get("Will you now or in the future require authorization to work in the United States?")).toBe("No");
    expect(byLabel.has("Are you willing to work on-site in our Las Vegas office 5 days a week?")).toBe(true);
    expect(byLabel.has("Keywords")).toBe(false);
  });

  it("the posting's labelled Location is a place the in-person questions compare (Las Vegas, from Denver)", () => {
    // Ashby's left pane: <h2>Location</h2><p>Las Vegas, Nevada</p>. It gave
    // the job's country and city but no listed place, and "our Las Vegas
    // office" names a city with no state, so the in-person rule had nothing
    // to compare and abstained for someone in Denver who will not move.
    document.body.innerHTML = `
      <div class="_left_5yu8i_421 ashby-job-posting-left-pane"><div class=" _section_f7cvd_36 "><h2 class="_heading_f7cvd_52 ">Location</h2><p>Las Vegas, Nevada</p></div><div class=" _section_f7cvd_36 "><h2 class="_heading_f7cvd_52 ">Employment Type</h2><p>Full time</p></div></div>
      ${PAGE}`;
    const place = detectJobPlace(document);
    expect(place.places).toEqual(["Las Vegas, NV, United States"]);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: "TensorWave" });
    const { fields } = scanPage(P.BOOTCAMP_CAREER_GAP as UserApplicationProfile, false, null);
    setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
    const byLabel = new Map(fields.map((f) => [f.label, f.proposedValue]));
    expect(byLabel.get("Are you willing to work on-site in our Las Vegas office 5 days a week?")).toBe("No");
  });
});

describe("'Where are you currently located?' is the applicant's location (TensorWave, ConductorAI)", () => {
  // The label alone never matched: "where are you located" did, "currently"
  // in between did not. It counted only where the box's name said
  // "_systemfield_location" (the bank's TensorWave and OpenAI); live, Ashby's
  // location typeahead has no name, and ConductorAI's box is named by an id.
  it.each([
    ["Where are you currently located?"],
    ["Where do you currently live?"],
    ["Where are you presently based?"],
  ])("%s", (label) => {
    document.body.innerHTML = `<form><label for="loc">${label}</label><input id="loc" type="text" name="5bc2dbb7-37f5-46a2-83ab-e52a8177f9be"><label for="em">Email</label><input id="em" type="email" name="email"></form>`;
    const { fields } = scanPage(P.BOOTCAMP_CAREER_GAP as UserApplicationProfile, false, null);
    const loc = fields.find((f) => f.label === label);
    expect(loc?.category).toBe("location");
    expect(loc?.proposedValue).toMatch(/^Denver\b/);
  });
});

describe("'Where do you currently live?' over states and countries (Squarespace, Greenhouse bank 1)", () => {
  const ask = (persona: UserApplicationProfile, label: string, options: string[]) => {
    const q: QuestionInput = { label, controlType: "select", options, category: "location", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "US", company: "Squarespace" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  // The list mixes US states with a few countries and an explicit "not
  // listed". It was blank for everyone: the rule matched a city, a country
  // or a continent, never a state, and never took the "not listed" option.
  const SQSP = ["Arizona", "California", "Colorado", "Connecticut", "Delaware", "Dublin, Ireland", "Florida", "Georgia", "Hawaii", "Ireland", "Kansas", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Missouri", "Netherlands", "New Jersey", "New York", "North Carolina", "Ohio", "Oregon", "Pennsylvania", "Portugal", "Tennessee", "Texas", "Utah", "Poland", "Virginia", "Washington", "Wisconsin", "Other - My state/country is not listed", "United Kingdom"];
  const Q = "Where do you currently live?";
  it("a US applicant's own state", () => {
    expect(ask(P.US_H1B_SENIOR, Q, SQSP)).toBe("Washington");
    expect(ask(P.BOOTCAMP_CAREER_GAP, Q, SQSP)).toBe("Colorado");
    expect(ask(P.US_OPT_ANALYST, Q, SQSP)).toBe("Massachusetts");
  });
  it("anyone whose state or country is not listed takes the list's own 'not listed'", () => {
    expect(ask(P.COMPLETE_CANADIAN, Q, SQSP)).toBe("Other - My state/country is not listed");
    expect(ask(P.BERLIN_STAFF, Q, SQSP)).toBe("Other - My state/country is not listed");
    expect(ask(P.INDIA_NEW_GRAD, Q, SQSP)).toBe("Other - My state/country is not listed");
  });
  it("without a 'not listed' option, an unlisted place stays blank", () => {
    expect(ask(P.COMPLETE_CANADIAN, Q, SQSP.filter((o) => !/not listed/.test(o)))).toBe("abstain");
  });
});

describe("a required mark is not a word of the question (Rhombus Power, Greenhouse bank 3)", () => {
  // "Where are you currently located? *" in a text area read as six words, so
  // as a prose prompt, and the city was kept out of it; without the mark it is
  // five words and a plain question.
  it("a required text area asking where you are located gets the location", () => {
    document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">Where are you currently located? *</label><textarea id="q0" name="question_31444299003"></textarea></div><div class="field"><label for="q1">Email*</label><input type="text" id="q1" name="email"></div></form>`;
    const { fields } = scanPage(P.BOOTCAMP_CAREER_GAP as UserApplicationProfile, false, null);
    expect(fields.find((f) => f.controlType === "textarea")?.proposedValue).toMatch(/^Denver\b/);
  });
});

describe("'Country - Region' places (Dropbox's Current Location, Greenhouse bank 3)", () => {
  const ask = (persona: UserApplicationProfile, label: string, options: string[]) => {
    const q: QuestionInput = { label, controlType: "select", options, category: "location", kind: "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "US", company: "Dropbox" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  const US = ["Alabama", "California", "Colorado", "Massachusetts", "New York", "Texas", "Washington", "Washington, D.C", "Wisconsin"].map((s) => `US - ${s}`);
  const CA = ["Alberta", "British Columbia", "Ontario", "Quebec"].map((s) => `Canada - ${s}`);
  const DBX = ["Australia", ...CA, "United Kingdom", "France", "Germany", "Ireland", "Japan", ...US, "Other Location"];
  const Q = "Current Location";
  it("the applicant's own country and region, never a namesake ('Washington, D.C')", () => {
    expect(ask(P.COMPLETE_CANADIAN, Q, DBX)).toBe("Canada - Ontario");
    expect(ask(P.US_H1B_SENIOR, Q, DBX)).toBe("US - Washington");
    expect(ask(P.BOOTCAMP_CAREER_GAP, Q, DBX)).toBe("US - Colorado");
    expect(ask(P.US_OPT_ANALYST, Q, DBX)).toBe("US - Massachusetts");
    expect(ask(P.BERLIN_STAFF, Q, DBX)).toBe("Germany");
    expect(ask(P.INDIA_NEW_GRAD, Q, DBX)).toBe("Other Location");
  });
  it("'Other Location' never stands in for a country the list names", () => {
    // Without Ontario, a Torontonian is in a listed country, not elsewhere.
    expect(ask(P.COMPLETE_CANADIAN, Q, DBX.filter((o) => o !== "Canada - Ontario"))).toBe("abstain");
  });
});

describe("Ashby's Yes/No questions answered for the first time, read in the regression (2026-10-08)", () => {
  const ask = (persona: UserApplicationProfile, label: string, options: string[]) => {
    const q: QuestionInput = { label, controlType: "radioGroup", options, category: "unknown", kind: options.length === 2 ? "boolean" : "choice" };
    const r = resolveQuestion(q, profileFacts(persona, TEST_TODAY), persona, { jobCountry: "US", company: "Acme" });
    return r && r.status === "answer" ? r.value : (r?.status ?? null);
  };
  it("several levels named are any of them (Ramp: a bachelor's student said No to 'Bachelor's, Master's, or PhD')", () => {
    const q = "Are you currently pursuing a Bachelor’s, Master’s, or PhD in Data Science, Computer Science, Math, Physics, Economics, Statistics, Engineering, or another quantitative field?";
    expect(ask(P.COMPLETE_CANADIAN as UserApplicationProfile, q, ["Yes", "No"])).toBe("Yes");
  });
  it("a field named with the degree: outside the sciences and engineering it is the applicant's", () => {
    const arts = { ...(P.COMPLETE_CANADIAN as UserApplicationProfile), education: [{ school: "University of Toronto", degree: "Bachelor of Arts in History", graduationYear: "2027" }], expectedGraduation: "2027-04" };
    expect(ask(arts, "Are you currently pursuing a degree in computer science or another related field?", ["Yes", "No"])).toBe("abstain");
  });
  it("a winter term is no month to place an April graduation in (Grow Therapy)", () => {
    const opts = ["Winter 2027", "Spring 2028", "Winter 2028", "Spring 2029", "Winter 2029", "Spring 2030", "Winter 2030", "I have already graduated"];
    expect(ask(P.COMPLETE_CANADIAN as UserApplicationProfile, "When is your expected graduation date?", opts)).not.toBe("Winter 2027");
  });
  it("paying for one's own housing and move is no willingness to move (The Exploration Company)", () => {
    const q = "If selected, will you be able to provide your own housing, relocation, and transportation to the internship site?";
    expect(ask(P.COMPLETE_CANADIAN as UserApplicationProfile, q, ["Yes", "No"])).not.toBe("Yes");
  });
});

describe("own housing and travel are no willingness to move, scanned (The Exploration Company, re-run 2026-10-08)", () => {
  // The question-path test above passed while the live page still wrote Yes:
  // "relocation" in the label made the field a willing-to-relocate one, and
  // that category answered from the profile before any rule read the rest.
  it("the field is no willing-to-relocate one, and stays blank", () => {
    const q = "If selected, will you be able to provide your own housing, relocation, and transportation to the internship site?";
    document.body.innerHTML = `<form><div class="field"><label for="q1">${q}</label><select id="q1"><option value="">Select...</option><option>Yes</option><option>No</option></select></div></form>`;
    setResolveContext({ jobCountry: "US", jobCity: null, jobPlaces: null, company: "The Exploration Company" });
    const [f] = scanPage(P.COMPLETE_CANADIAN as UserApplicationProfile, true).fields;
    expect(f.category).not.toBe("willingToRelocate");
    expect(f.proposedValue ?? null).toBeNull();
  });
  it("relocation at one's own expense is the applicant's too", () => {
    const q = "Are you willing to relocate to Austin, TX at your own expense?";
    document.body.innerHTML = `<form><div class="field"><label for="q1">${q}</label><select id="q1"><option value="">Select...</option><option>Yes</option><option>No</option></select></div></form>`;
    setResolveContext({ jobCountry: "US", jobCity: "Austin", jobPlaces: null, company: "Acme" });
    const [f] = scanPage(P.COMPLETE_CANADIAN as UserApplicationProfile, true).fields;
    expect(f.proposedValue ?? null).toBeNull();
  });
  it("a plain willingness to relocate still answers from the profile", () => {
    document.body.innerHTML = `<form><div class="field"><label for="q1">Are you willing to relocate?</label><select id="q1"><option value="">Select...</option><option>Yes</option><option>No</option></select></div></form>`;
    setResolveContext({ jobCountry: "US", jobCity: null, jobPlaces: null, company: "Acme" });
    const [f] = scanPage(P.COMPLETE_CANADIAN as UserApplicationProfile, true).fields;
    expect(f.category).toBe("willingToRelocate");
    expect(f.proposedValue).toBe("Yes");
  });
});

describe("a web component's own label attribute names its field (SmartRecruiters, regression 2026-10-08)", () => {
  // <spl-input label="First name"> renders its <label> inside its shadow
  // root. A scan taken before that render (a document observer never sees a
  // shadow root change) read every field by its id: "first-name-input",
  // "spl-form-element_10" for City, which went unrecognized and blank.
  const host = (tag: string, label: string, inputId: string): HTMLElement => {
    const el = document.createElement(tag);
    el.setAttribute("label", label);
    const root = el.attachShadow({ mode: "open" });
    root.innerHTML = `<div class="field"><input id="${inputId}" type="text"></div>`;
    return el;
  };
  const scan = () => {
    setResolveContext({ jobCountry: "US", jobCity: null, jobPlaces: null, company: "ServiceNow" });
    return scanPage(P.COMPLETE_CANADIAN as UserApplicationProfile, true).fields;
  };
  it("reads the host's label when the shadow root has none yet", () => {
    document.body.innerHTML = "<form></form>";
    const form = document.querySelector("form")!;
    form.append(host("spl-input", "First name", "first-name-input"), host("spl-autocomplete", "City", "spl-form-element_10"));
    const fields = scan();
    expect(fields.map((f) => f.label)).toEqual(["First name", "City"]);
    expect(fields.map((f) => f.category)).toEqual(["firstName", "addressCity"]);
  });
  it("a label the shadow root does hold still wins", () => {
    document.body.innerHTML = "<form></form>";
    const el = host("spl-input", "Name", "n1");
    el.shadowRoot!.innerHTML = `<label for="n1">Last name</label><input id="n1" type="text">`;
    document.querySelector("form")!.append(el);
    expect(scan().map((f) => f.label)).toEqual(["Last name"]);
  });
});

describe("agreeing to arbitrate is the applicant's (decision 51, 2026-10-08)", () => {
  // Giving up the right to sue (arbitration, a jury trial, a class action)
  // is the applicant's to accept, and never the AI's. The banks showed both:
  // Block's and sweetgreen's agreements were left blank, Anthropic's,
  // Roblox's and Asana's accepted. Acknowledging receipt, or that one will
  // read it, gives nothing up; neither does an application certification.
  const scanOne = (label: string, options: string[]) => {
    const opts = options.map((o) => `<option>${o}</option>`).join("");
    document.body.innerHTML = `<form><div class="field"><label for="q1">${label}</label><select id="q1"><option value="">Select...</option>${opts}</select></div></form>`;
    setResolveContext({ jobCountry: "US", jobCity: null, jobPlaces: null, company: "Acme" });
    const [f] = scanPage(P.COMPLETE_CANADIAN as UserApplicationProfile, true).fields;
    return f;
  };
  it("an arbitration agreement is left blank, and kept from the AI", () => {
    const roblox = scanOne("Please review and acknowledge Roblox's Candidate Arbitration Agreement", [
      "I acknowledge that I received and had the opportunity to review Roblox's Candidate Arbitration Agreement, and I agree to be bound by its terms.",
    ]);
    expect(roblox.proposedValue ?? null).toBeNull();
    expect(roblox.deviceAbstained).toBe(true);
    const anthropic = scanOne("Agreement to Arbitrate", ["I understand and agree to the terms of the Agreement to Arbitrate set forth above."]);
    expect(anthropic.proposedValue ?? null).toBeNull();
    const asana = scanOne(
      "By agreeing here: (1) I certify that the information I provided to Asana in connection with my application for employment is accurate and truthful; and (2) I acknowledge and agree that I have carefully read the mutual Applicant Arbitration Agreement set out below and agree to be bound by it",
      ["Yes", "No"]
    );
    expect(asana.proposedValue ?? null).toBeNull();
  });
  it("so is a lone box agreeing to it", () => {
    document.body.innerHTML = `<form><label><input type="checkbox" id="c1"> I have read and agree to the Mutual Arbitration Agreement, including the waiver of class actions.</label></form>`;
    setResolveContext({ jobCountry: "US", jobCity: null, jobPlaces: null, company: "Acme" });
    const [f] = scanPage(P.COMPLETE_CANADIAN as UserApplicationProfile, true).fields;
    expect(f.proposedValue === "yes").toBe(false);
  });
  it("receiving it, or reading it, gives nothing up", () => {
    expect(scanOne("Please read the arbitration agreement below", ["I will read the arbitration agreement below."]).proposedValue).toBe("I will read the arbitration agreement below.");
    expect(scanOne("Please confirm receipt of the above linked Global Data Privacy Notice and US Arbitration Agreement.", ["Confirmed"]).proposedValue).toBe("Confirmed");
  });
});
