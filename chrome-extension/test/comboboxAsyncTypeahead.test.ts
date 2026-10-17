// chrome-extension/test/comboboxAsyncTypeahead.test.ts
/**
 * A remote typeahead must not be abandoned while its request is in flight.
 *
 * Greenhouse's School picker searches its institution list server-side: the
 * instant you type, react-select DROPS its options and renders a
 * `select__menu-notice--loading` div, which is not a `[role="option"]`. The
 * poll loop read that empty list as "the filter answered and settled", gave up
 * three polls later, and restored the input to blank.
 *
 * The user saw exactly that on a real Lyft application (autofill_reports #168,
 * 2026-08-13): "on the school field it initially put University of Ottawa but
 * then removed it". Its neighbour Discipline was fine, because that list is
 * small and local and answers on the first poll.
 *
 * Latency here is measured in POLLS, not milliseconds: the engine's `sleep` is
 * injectable, so the fixture delivers its "response" on the Nth sleep. That
 * models "results arrive several polls after typing" without a wall clock, so
 * the test cannot flake on a slow machine.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "vitest";
import { fillAriaCombobox } from "../src/content/comboboxEngine";
import { stubLayout } from "./helpers/layout";

let restoreLayout: () => void;
beforeAll(() => { restoreLayout = stubLayout(); });
afterAll(() => restoreLayout());
beforeEach(() => { document.body.innerHTML = ""; });

const SCHOOLS = ["University of Ottawa", "University of Toronto", "McGill University"];

/**
 * A react-select remote typeahead.
 *
 * `openWith: []` is the faithful default for a server-side search: the menu
 * mounts EMPTY, because the widget has nothing to show until you type. Passing
 * options renders them on open, which models a widget that ships a default list
 * and then re-filters remotely: the two shapes exit the poll loop by different
 * routes (reaction window vs stability counter), so both are worth covering.
 */
function remoteTypeahead(
  options: string[],
  latencyPolls: number,
  openWith: string[] = [],
  // Greenhouse's school search lists nothing until something is typed.
  searchOnly = false,
  // The search itself, when it is no substring filter.
  search?: (q: string) => string[]
) {
  const control = document.createElement("div");
  control.className = "select__control";
  const single = document.createElement("div");
  single.className = "select__single-value";
  const input = document.createElement("input");
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-haspopup", "listbox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-autocomplete", "list");
  const lbId = `lb-${Math.random().toString(36).slice(2)}`;
  input.setAttribute("aria-controls", lbId);
  control.append(single, input);
  document.body.append(control);

  const menu = (): HTMLElement => {
    let lb = document.getElementById(lbId);
    if (!lb) {
      lb = document.createElement("div");
      lb.id = lbId;
      lb.setAttribute("role", "listbox");
      control.append(lb);
    }
    return lb;
  };

  const renderOptions = (labels: string[]): void => {
    const lb = menu();
    lb.innerHTML = "";
    for (const label of labels) {
      const o = document.createElement("div");
      o.setAttribute("role", "option");
      o.setAttribute("aria-selected", "false");
      o.textContent = label;
      o.addEventListener("mousedown", () => {
        single.textContent = label;
        input.value = "";
        input.setAttribute("aria-expanded", "false");
        lb.remove(); // react-select unmounts the menu on select
      });
      lb.append(o);
    }
  };

  /** What react-select shows while the request is in flight: NOT a role=option. */
  const renderLoading = (): void => {
    menu().innerHTML =
      `<div class="select__menu-notice select__menu-notice--loading">Loading...</div>`;
  };

  let pending: string | null = null;
  let ticksLeft = 0;

  input.addEventListener("mousedown", () => {
    input.setAttribute("aria-expanded", "true");
    renderOptions(openWith); // a server-side search mounts its menu empty
  });
  input.addEventListener("input", () => {
    pending = input.value.trim().toLowerCase();
    ticksLeft = latencyPolls;
    renderLoading(); // options vanish the moment you type
  });

  /** Injected as the engine's sleep: one call = one poll of elapsed time. */
  const sleep = async (): Promise<void> => {
    if (pending === null) return;
    if (--ticksLeft > 0) return;
    const q = pending;
    pending = null;
    renderOptions(search ? search(q) : q ? options.filter((o) => o.toLowerCase().includes(q)) : searchOnly ? [] : options);
  };

  return { input, single, sleep };
}

// With an injected sleep these are poll COUNTS, not real time: 2000/10 = 200
// polls of budget. The budget has to exceed the engine's reaction window, which
// is capped at 800ms (80 polls here); with a smaller budget the two coincide
// and a premature exit is indistinguishable from the hard timeout.
const opts = (sleep: () => Promise<void>) => ({
  sleep, openWaitMs: 2000, commitWaitMs: 400, pollMs: 10,
});
/** Past the 80-poll reaction window, still inside the 200-poll budget. */
const SLOW = 120;

describe("remote typeahead: options arrive after a round trip", () => {
  it("waits for the results instead of wiping what it typed", async () => {
    // Exit route 1: the menu was empty before AND after typing, so the list
    // never "reacts" and the 800ms reaction window used to end the attempt.
    const { input, single, sleep } = remoteTypeahead(SCHOOLS, SLOW);
    const res = await fillAriaCombobox(input, "University of Ottawa", opts(sleep));
    expect(res.reason ?? "").not.toMatch(/No option matches/);
    expect(res.filled).toBe(true);
    expect(single.textContent).toBe("University of Ottawa");
  });

  it("waits when the menu had default options and then emptied to re-search", async () => {
    // Exit route 2: the list DID react (it emptied), so the stability counter
    // used to end the attempt three polls later.
    const { input, single, sleep } = remoteTypeahead(SCHOOLS, SLOW, ["Popular: McGill University"]);
    const res = await fillAriaCombobox(input, "University of Ottawa", opts(sleep));
    expect(res.filled).toBe(true);
    expect(single.textContent).toBe("University of Ottawa");
  });

  it("still gives up when the list answers and genuinely has no match", async () => {
    // The early exit has to survive: a filter that returns real options and
    // then holds steady IS the widget's final answer, and waiting out the full
    // budget for it is the dead time the user is complaining about.
    const { input, sleep } = remoteTypeahead(["McGill University"], 2);
    const res = await fillAriaCombobox(input, "University of Ottawa", opts(sleep));
    expect(res.filled).toBe(false);
    expect(res.reason).toMatch(/No option matches/);
  });

  it("reports the options it did see, so the field can be re-asked", async () => {
    const { input, sleep } = remoteTypeahead(["McGill University"], 2);
    const res = await fillAriaCombobox(input, "University of Ottawa", opts(sleep));
    expect(res.options).toContain("McGill University");
  });
});

/**
 * Greenhouse's school search matches what is typed as a substring of its own
 * names (Twitch, SpaceX, live 2026-10-03): "University of Texas at Austin"
 * finds nothing, because the list says "University of Texas - Austin"; "San
 * José State University" finds nothing, because the list has no accent. The
 * names below are the live search results.
 */
describe("a school searched the way the list spells it (Greenhouse, live 2026-10-03)", () => {
  const LIST = [
    "University of Texas - Arlington", "University of Texas - Austin", "University of Texas - Brownsville", "University of Texas - Dallas",
    "University of Texas - El Paso", "University of Texas Health Science Center - Houston", "University of Texas - San Antonio",
    "University of Texas - Tyler", "San Jose State University", "San Jose City College",
  ];

  it("'The University of Texas at Austin' finds 'University of Texas - Austin' by its leading words", async () => {
    const { input, single, sleep } = remoteTypeahead(LIST, 3, [], true);
    const res = await fillAriaCombobox(input, "The University of Texas at Austin", opts(sleep));
    expect(res.filled).toBe(true);
    expect(single.textContent).toBe("University of Texas - Austin");
  });

  it("'San José State University' is searched without the accent", async () => {
    const { input, single, sleep } = remoteTypeahead(LIST, 3, [], true);
    const res = await fillAriaCombobox(input, "San José State University", opts(sleep));
    expect(res.filled).toBe(true);
    expect(single.textContent).toBe("San Jose State University");
  });

  it("a campus the list does not have is still no match", async () => {
    const { input, single, sleep } = remoteTypeahead(LIST, 3, [], true);
    const res = await fillAriaCombobox(input, "University of Texas at Midland", opts(sleep));
    expect(res.filled).toBe(false);
    expect(single.textContent).toBe("");
  });

  // Everpure (live 2026-10-17): the search's answer to the full name held
  // other schools of Austin and not the university, and a shared word chose
  // "Austin Peay State University". A school is chosen by its own words only.
  it("never takes another school that shares a word, and searches on", async () => {
    const search = (q: string): string[] =>
      /austin/.test(q) ? ["Austin Peay State University", "Austin College"] : /^university of texas$/.test(q) ? LIST.slice(0, 8) : [];
    const { input, single, sleep } = remoteTypeahead(LIST, 3, [], true, search);
    const res = await fillAriaCombobox(input, "The University of Texas at Austin", { ...opts(sleep), school: true });
    expect(single.textContent).toBe("University of Texas - Austin");
    expect(res.filled).toBe(true);
  });
  it("and leaves it blank when no answer holds the school", async () => {
    const search = (q: string): string[] => (/austin|texas|university/.test(q) ? ["Austin Peay State University", "University of Texas - Arlington"] : []);
    const { input, single, sleep } = remoteTypeahead(LIST, 3, [], true, search);
    const res = await fillAriaCombobox(input, "The University of Texas at Austin", { ...opts(sleep), school: true });
    expect(res.filled).toBe(false);
    expect(single.textContent).toBe("");
  });
});

/**
 * Ashby's school search (Ramp, live 2026-10-17): each result is the school's
 * name, its country and its domain, three boxes in flex rows, so the option's
 * text runs "University of WaterlooCanadauwaterloo.ca". The name is the first
 * box. Markup verbatim from the live page (class names shortened by nothing).
 */
describe("a school result with its country and domain (Ashby, live 2026-10-17)", () => {
  const STYLE = `<style>._stack_b7xpf_1{display:flex}._vertical_b7xpf_4{flex-direction:column}</style>`;
  const result = (name: string, country: string, domain: string, i: number): string =>
    `<div role="option" id=":r${i}:" aria-selected="false" class="_result_d7ago_107 ashby-application-form-input-autocomplete-popup-result">` +
    `<div class="_stack_b7xpf_1 _vertical_b7xpf_4 _gapXxsmall_b7xpf_10 _lineHeightInitial_1e3gg_201">` +
    `<div class="_stack_b7xpf_1 _horizontal_b7xpf_7 _gapSmall_b7xpf_16 _justifyBetween_b7xpf_22 _alignCenter_b7xpf_25 _lineHeightInitial_1e3gg_201">` +
    `<span class="_canonicalSchoolResultName_1e3gg_205">${name}</span><span class="_canonicalSchoolResultCountry_1e3gg_209">${country}</span></div>` +
    `<span class="_canonicalSchoolResultDomain_1e3gg_218">${domain}</span></div></div>`;
  const RESULTS: [string, string, string][] = [
    ["University of Waterloo", "Canada", "uwaterloo.ca"],
    ["University of Wah", "Pakistan", "uow.edu.pk"],
    ["University of Wasit", "Iraq", "uowasit.edu.iq"],
    ["University of Wales", "United Kingdom", "wales.ac.uk"],
  ];
  function ashbySchool(results: [string, string, string][]) {
    document.head.innerHTML = STYLE;
    document.body.innerHTML =
      `<div class="_inputContainer_d7ago_28"><input class="_input_d7ago_28 ashby-application-form-input-autocomplete" placeholder="Search schools..." aria-autocomplete="list" aria-expanded="false" aria-haspopup="listbox" role="combobox" aria-controls=":r0:" value=""></div>`;
    const input = document.querySelector("input")!;
    let chosen = "";
    input.addEventListener("input", () => {
      input.setAttribute("aria-expanded", "true");
      let lb = document.getElementById(":r0:");
      if (!lb) {
        lb = document.createElement("div");
        lb.id = ":r0:";
        lb.setAttribute("role", "listbox");
        document.body.append(lb);
      }
      lb.innerHTML = input.value.trim() ? results.map((r, i) => result(...r, i + 3)).join("") : "";
      for (const o of lb.querySelectorAll<HTMLElement>('[role="option"]')) {
        o.addEventListener("mousedown", () => {
          chosen = o.querySelector("span")!.textContent!;
          input.value = chosen;
          input.setAttribute("aria-expanded", "false");
          lb!.remove();
        });
      }
    });
    return { input, chosen: () => chosen };
  }
  const quick = { openWaitMs: 300, commitWaitMs: 300, pollMs: 10 };

  it("takes the result whose name is the school", async () => {
    const { input, chosen } = ashbySchool(RESULTS);
    const res = await fillAriaCombobox(input, "University of Waterloo", { ...quick, school: true });
    expect(chosen()).toBe("University of Waterloo");
    expect(res.filled).toBe(true);
  });
  it("and none of the others", async () => {
    const { input, chosen } = ashbySchool(RESULTS.slice(1));
    const res = await fillAriaCombobox(input, "University of Waterloo", { ...quick, school: true });
    expect(chosen()).toBe("");
    expect(res.filled).toBe(false);
  });
});
