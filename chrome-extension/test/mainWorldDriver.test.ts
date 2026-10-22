import { describe, it, expect, beforeEach } from "vitest";
import { installDriver, pickOption, __test } from "../src/content/mainWorldDriver";
import { MW_FILL_EVENT, MW_RESULT_EVENT, type MwResultDetail } from "../src/content/mainWorldBridge";
import { FIELD_ID_ATTR } from "../src/shared/constants";

beforeEach(() => {
  document.body.innerHTML = "";
  __test.uninstall(window);
});

function drive(fieldId: string, value: string, kind: "react-select" | "workday"): Promise<MwResultDetail> {
  return new Promise((resolve) => {
    const onResult = (e: Event): void => {
      const d = (e as CustomEvent<MwResultDetail>).detail;
      if (d.id !== 99) return;
      window.removeEventListener(MW_RESULT_EVENT, onResult);
      resolve(d);
    };
    window.addEventListener(MW_RESULT_EVENT, onResult);
    window.dispatchEvent(new CustomEvent(MW_FILL_EVENT, { detail: { id: 99, fieldId, value, kind } }));
  });
}

describe("pickOption for a school (Everpure on Greenhouse, live 2026-10-17)", () => {
  it("takes the school by its own words, never one that shares a word", () => {
    const v = "The University of Texas at Austin";
    expect(pickOption(["Austin Peay State University", "Austin College"], v, undefined, true)).toBe(-1);
    expect(pickOption(["University of Texas - Arlington", "Austin Peay State University"], v, undefined, true)).toBe(-1);
    expect(pickOption(["Austin Peay State University", "University of Texas - Austin"], v, undefined, true)).toBe(1);
  });
});

describe("pickOption", () => {
  it("prefers exact, then contains, then token overlap", () => {
    expect(pickOption(["United States", "Canada"], "Canada")).toBe(1);
    expect(pickOption(["Yes", "No"], "Yes, I am authorized")).toBe(0);
    expect(pickOption(["Bachelor of Science", "Master of Science"], "master science")).toBe(1);
    expect(pickOption(["A", "B"], "Zorp")).toBe(-1);
  });

  it("never picks a DIFFERENT university on the shared 'University' token", () => {
    // The random-university bug: every school option overlaps on "university",
    // so single-token overlap must not select anything.
    expect(
      pickOption(["University of Oklahoma", "University of Texas", "Oklahoma State University"], "University of Ottawa")
    ).toBe(-1);
    // …while the actual school still matches when present.
    expect(
      pickOption(["University of Oklahoma", "University of Ottawa"], "University of Ottawa")
    ).toBe(1);
  });

  it("is the content script's strict matcher, not a looser copy (Greenhouse, live 2026-10-03)", () => {
    // Graduation year "2027" fits two of ZipRecruiter's buckets; the old
    // first-containing-option rule committed the first one.
    const grad = ["I have already graduated", "December 2026 - November 2027", "December 2027 - November 2028", "December 2028 - November 2029"];
    expect(pickOption(grad, "2027")).toBe(-1);
    expect(pickOption(grad, "December 2027 - November 2028")).toBe(2);
    // Substring containment: "male" is inside "female".
    expect(pickOption(["Female", "Male (he/him)"], "Male")).toBe(1);
    // A negated answer never lands on the affirmative option.
    expect(pickOption(["I am a protected veteran", "I am not a protected veteran"], "I am not a veteran")).toBe(1);
  });
});

describe("installDriver", () => {
  it("installs once (guard) and ignores a second install", () => {
    installDriver(window);
    installDriver(window); // must not double-register
    expect((window as unknown as Record<string, unknown>).__tailrdMWInstalled).toBe(true);
  });

  it("replies not-ok when the field id is missing", async () => {
    installDriver(window);
    const res = await drive("nope-1", "Canada", "react-select");
    expect(res.ok).toBe(false);
  });
});

/** react-select container with a mock Fiber exposing selectOption(). */
function reactSelectWithFiber(fieldId: string, options: string[]): { display: HTMLElement } {
  const container = document.createElement("div");
  container.className = "rs__container";
  container.setAttribute(FIELD_ID_ATTR, fieldId);
  const control = document.createElement("div");
  control.className = "rs__control";
  const single = document.createElement("div");
  single.className = "rs__single-value";
  const input = document.createElement("input");
  input.id = "react-select-9-input";
  input.setAttribute("role", "combobox");
  control.append(single, input);
  container.append(control);
  document.body.append(container);

  const opts = options.map((label) => ({ label, value: label }));
  const instance = {
    props: { options: opts, getOptionLabel: (o: { label: string }) => o.label },
    selectOption: (o: { label: string }) => { single.textContent = o.label; },
  };
  const fiber = { return: null, stateNode: instance, memoizedProps: instance.props };
  (container as unknown as Record<string, unknown>)["__reactFiber$abc"] = fiber;
  return { display: single };
}

describe("fillReactSelect via Fiber", () => {
  it("calls selectOption for the matching option and reports committed text", async () => {
    installDriver(window);
    const { display } = reactSelectWithFiber("rs-1", ["United States", "Canada", "Mexico"]);
    const res = await drive("rs-1", "Canada", "react-select");
    expect(res.ok).toBe(true);
    expect(display.textContent).toBe("Canada");
    expect(res.committed).toBe("Canada");
  });

  it("reports no-match when the option is absent", async () => {
    installDriver(window);
    reactSelectWithFiber("rs-2", ["United States", "Canada"]);
    const res = await drive("rs-2", "Atlantis", "react-select");
    expect(res.ok).toBe(false);
    expect(res.reason).toBe("no-match");
  });

  it("hands back the options it saw on a miss, for the re-ask to choose from", async () => {
    // Epic Games' Degree (react-select v3, live 2026-10-05): "Certificate" is
    // not in the list, and the fallback that followed could not open the
    // widget, so nothing was ever chosen from the real options.
    installDriver(window);
    reactSelectWithFiber("rs-3", ["High School", "Associate's Degree", "Bachelor's Degree", "Other"]);
    const res = await drive("rs-3", "Certificate", "react-select");
    expect(res.ok).toBe(false);
    expect(res.options).toEqual(["High School", "Associate's Degree", "Bachelor's Degree", "Other"]);
  }, 10_000);
});

/** Workday prompt: button + on-click list of [data-automation-id=promptOption]. */
function workdayPrompt(fieldId: string, options: string[]): HTMLElement {
  const wrap = document.createElement("div");
  wrap.setAttribute("data-automation-id", "multiSelectContainer");
  wrap.setAttribute(FIELD_ID_ATTR, fieldId);
  const btn = document.createElement("button");
  btn.setAttribute("data-automation-id", "promptButton");
  btn.textContent = "Select One";
  wrap.append(btn);
  document.body.append(wrap);

  btn.addEventListener("mousedown", () => {
    if (wrap.querySelector('[data-automation-id="promptOption"]')) return;
    for (const label of options) {
      const o = document.createElement("div");
      o.setAttribute("data-automation-id", "promptOption");
      o.textContent = label;
      o.addEventListener("mousedown", () => { btn.textContent = label; o.parentElement?.querySelectorAll('[data-automation-id="promptOption"]').forEach((n) => n.remove()); });
      wrap.append(o);
    }
  });
  return wrap;
}

describe("fillWorkday via DOM prompt", () => {
  it("opens the prompt and selects the matching option", async () => {
    installDriver(window);
    const wrap = workdayPrompt("wd-1", ["United States", "Canada"]);
    const res = await drive("wd-1", "Canada", "workday");
    expect(res.ok).toBe(true);
    expect(wrap.querySelector('[data-automation-id="promptButton"]')?.textContent).toBe("Canada");
  });
});

describe("fillWorkday via Fiber onChange", () => {
  it("invokes the widget's onChange with the matched option", async () => {
    installDriver(window);
    const wrap = document.createElement("div");
    wrap.setAttribute("data-automation-id", "selectinput");
    wrap.setAttribute(FIELD_ID_ATTR, "wd-2");
    document.body.append(wrap);
    let chosen: string | null = null;
    const props = {
      options: [{ label: "Female", value: "f" }, { label: "Male", value: "m" }],
      onChange: (o: { label: string }) => { chosen = o.label; },
    };
    (wrap as unknown as Record<string, unknown>)["__reactFiber$z"] = { return: null, stateNode: null, memoizedProps: props };
    const res = await drive("wd-2", "Female", "workday");
    expect(res.ok).toBe(true);
    expect(chosen).toBe("Female");
  });
});

/**
 * Greenhouse's education School, Degree and Discipline (remix react-selects,
 * live, round 8): the list is supplied only when the menu OPENS (a mousedown
 * on the control, as a person's click). Typed into while closed, nothing
 * loads: the driver waited out 3.5 s on each, then the ARIA fallback raced the
 * same lazy list, and under a batch of live pages it lost ("Couldn't open the
 * 'Bachelor' dropdown" on GCM Grosvenor; its Discipline the next time).
 */
function lazyReactSelect(fieldId: string, firstPage: string[], search: (q: string) => string[]): { display: HTMLElement; instance: { props: Record<string, unknown> } } {
  const container = document.createElement("div");
  container.className = "select__container";
  container.setAttribute(FIELD_ID_ATTR, fieldId);
  const control = document.createElement("div");
  control.className = "select__control";
  const single = document.createElement("div");
  single.className = "select__single-value";
  const input = document.createElement("input");
  input.id = `${fieldId}-input`;
  input.setAttribute("role", "combobox");
  control.append(single, input);
  container.append(control);
  document.body.append(container);
  const instance = {
    props: { options: [] as { label: string; value: string }[], isLoading: false, menuIsOpen: false, getOptionLabel: (o: { label: string }) => o.label } as Record<string, unknown>,
    selectOption: (o: { label: string }) => {
      single.textContent = o.label;
      instance.props = { ...instance.props, menuIsOpen: false };
    },
  };
  const set = (labels: string[]): void => {
    instance.props = { ...instance.props, options: labels.map((label) => ({ label, value: label })), isLoading: false };
  };
  control.addEventListener("mousedown", () => {
    if (instance.props.menuIsOpen) return;
    instance.props = { ...instance.props, menuIsOpen: true, isLoading: true };
    setTimeout(() => set(firstPage), 120);
  });
  // A search runs only while the menu is open; typed into a closed menu, the
  // input changes and nothing loads.
  input.addEventListener("input", () => {
    if (!instance.props.menuIsOpen) return;
    instance.props = { ...instance.props, isLoading: true };
    setTimeout(() => set(input.value ? search(input.value) : firstPage), 120);
  });
  const fiber = { return: null, stateNode: instance, memoizedProps: instance.props };
  (container as unknown as Record<string, unknown>)["__reactFiber$abc"] = fiber;
  return { display: single, instance };
}

describe("fillReactSelect: a list supplied when its menu opens (Greenhouse's education, round 8)", () => {
  const DEGREES = ["Associate's Degree", "Bachelor's Degree", "Master's Degree", "Doctor of Philosophy (Ph.D.)", "Engineer's Degree", "High School"];
  it("opens the menu, then picks from the list it loads", async () => {
    installDriver(window);
    const { display } = lazyReactSelect("rs-lazy", DEGREES, (q) => DEGREES.filter((d) => d.toLowerCase().includes(q.toLowerCase())));
    const t0 = Date.now();
    const res = await drive("rs-lazy", "Bachelor", "react-select");
    expect(res.ok).toBe(true);
    expect(display.textContent).toBe("Bachelor's Degree");
    // No 3.5 s spent typing into a closed menu.
    expect(Date.now() - t0).toBeLessThan(2000);
  });
  it("searches the open menu when the first page does not hold the answer", async () => {
    installDriver(window);
    const schools = ["Aalborg University", "Aalto University", "Aarhus University"];
    const { display } = lazyReactSelect("rs-lazy-school", schools, (q) => (/waterloo/i.test(q) ? ["University of Waterloo", "Wilfrid Laurier University (Waterloo)"] : []));
    const res = await drive("rs-lazy-school", "University of Waterloo", "react-select");
    expect(res.ok).toBe(true);
    expect(display.textContent).toBe("University of Waterloo");
  });
});
