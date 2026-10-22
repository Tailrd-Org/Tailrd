/**
 * A self-identification list picked in the widget by the self-ID rules
 * (demographicMatch.ts), never by a word an option shares. Gusto's "I identify
 * my race/ethnicity as (mark all that apply)" (Greenhouse remix multi-select,
 * live, round 8) took "East Asian (inclusive of …)" for a stated "Asian": the
 * shortest of three options holding the word, where the scan's own matcher
 * leaves the choice to the applicant.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { pickOption } from "../src/content/mainWorldDriver";
import { fillAriaCombobox } from "../src/content/comboboxEngine";
import { closestDemographicOption, declineOption } from "../src/content/demographicMatch";
import { stubLayout } from "./helpers/layout";

// Verbatim from Gusto's posting (Greenhouse job board API, 2026-10-22).
const GUSTO_RACE = [
  "Alaskan Native / American Indian / Indigenous American / Native American",
  "Black / of African or African-American descent",
  "East Asian (inclusive of Chinese, Japanese, Korean, Mongolian, Tibetan, and Taiwanese)",
  "Latino/a/x/Hispanic (inclusive of the United States of America, South America, Central America, and the Caribbean)",
  "Latino/a/x/Hispanic (inclusive of Canada, South America, Central America, and the Caribbean)",
  "Middle Eastern",
  "Pacific Islander or Native Hawaiian",
  "South Asian (inclusive of Afghani, Bangladeshi, Bhutanese, Indian, Nepali, Pakistani, and Sri Lankan)",
  "Southeast Asian (inclusive of Burmese, Cambodian, Filipino, Hmong, Indonesian, Laotian, Malaysian, Mien, Singaporean, Thai, and Vietnamese)",
  "White",
  "I prefer to self-describe",
  "I don't wish to answer",
];

describe("the page-world driver's pick for a self-ID list", () => {
  it("a stated race several options narrow is no pick", () => {
    expect(pickOption(GUSTO_RACE, "Asian", undefined, false, "eeoRace")).toBe(-1);
    // The same list in the lower case the DOM fallback reads it in.
    expect(pickOption(GUSTO_RACE.map((o) => o.toLowerCase()), "Asian", undefined, false, "eeoRace")).toBe(-1);
  });
  it("the one option that is, or holds, the stated answer is", () => {
    expect(pickOption(GUSTO_RACE, "White", undefined, false, "eeoRace")).toBe(9);
    expect(pickOption(GUSTO_RACE, "Black", undefined, false, "eeoRace")).toBe(1);
  });
});

let restoreLayout: () => void;
beforeAll(() => {
  restoreLayout = stubLayout();
});
afterAll(() => restoreLayout());
beforeEach(() => {
  document.body.innerHTML = "";
});

/** A react-select-style combobox: its menu renders on mousedown, an option's
 *  mousedown commits it into `.select__single-value`. */
function reactSelect(options: string[]): { input: HTMLInputElement; shown: () => string } {
  const control = document.createElement("div");
  control.className = "select__control";
  const single = document.createElement("div");
  single.className = "select__single-value";
  const input = document.createElement("input");
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-haspopup", "listbox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-autocomplete", "list");
  const listboxId = `lb-${Math.random().toString(36).slice(2)}`;
  input.setAttribute("aria-controls", listboxId);
  control.append(single, input);
  document.body.append(control);
  input.addEventListener("mousedown", () => {
    input.setAttribute("aria-expanded", "true");
    if (document.getElementById(listboxId)) return;
    const lb = document.createElement("div");
    lb.id = listboxId;
    lb.setAttribute("role", "listbox");
    for (const label of options) {
      const o = document.createElement("div");
      o.setAttribute("role", "option");
      o.setAttribute("aria-selected", "false");
      o.textContent = label;
      o.addEventListener("mousedown", () => {
        single.textContent = label;
        input.value = "";
        input.setAttribute("aria-expanded", "false");
        lb.remove();
      });
      lb.append(o);
    }
    control.append(lb);
  });
  input.addEventListener("keydown", (e) => {
    if ((e as KeyboardEvent).key === "Escape") {
      input.setAttribute("aria-expanded", "false");
      document.getElementById(listboxId)?.remove();
    }
  });
  return { input, shown: () => single.textContent ?? "" };
}

const fast = { sleep: async (): Promise<void> => {}, openWaitMs: 200, commitWaitMs: 200, pollMs: 10 };

describe("the combobox engine's pick for a self-ID list", () => {
  it("a stated race several options narrow is left, with the real options", async () => {
    const { input, shown } = reactSelect(GUSTO_RACE);
    const res = await fillAriaCombobox(input, "Asian", { ...fast, demographic: "eeoRace" });
    expect(res.filled).toBe(false);
    expect(shown()).toBe("");
    expect(res.options?.length).toBeGreaterThan(0);
  });
  it("the one option that is the stated answer is picked", async () => {
    const { input, shown } = reactSelect(GUSTO_RACE);
    const res = await fillAriaCombobox(input, "White", { ...fast, demographic: "eeoRace" });
    expect(res.filled).toBe(true);
    expect(shown()).toBe("White");
  });
});

describe("the self-ID matcher on lists with descriptions (Greenhouse bank 3, round 8)", () => {
  // Verbatim from Webflow's and Hootsuite's postings (question bank 3).
  const WEBFLOW = ["American Indian or Alaska Native - A person having origins in any of the original peoples of North and South America (including Central America) and who maintain tribal affiliation or community attachment", "Asian - A person having origins in any of the original peoples of the Far East, Southeast Asia, or the Indian subcontinent", "Black - A person having origins in any of the Black racial groups of Africa", "Hispanic or LatinX  - A person of Cuban, Mexican, Puerto Rican, South or Central American, or other Spanish culture or origin regardless of race", "Native Hawaiian or Other Pacific Islander- A person having origins in any of the original peoples of Hawaii, Guam, Samoa, or other Pacific Islands", "Two or More Races - refers to 2 or more races other than White also known as multiracial", "White - A person having origins in any of the original people of Europe, the Middle East, or North Africa", "I do not wish to disclose", "Not Specified"];
  const HOOTSUITE = ["Legally Blind, Low Vision, partially sighted and or totally Blind", "Episodic disabilities (Chronic illness or pain)", "Deaf, hearing loss", "Other", "Yes, I have a disability but do not want to provide specifics", "No, I don’t have a disability", "Prefer not to answer"];
  it("the option that starts with the answer, where another names it in passing", () => {
    // White was left blank: "Two or More Races … other than White" holds it too.
    expect(closestDemographicOption("eeoRace", "White", WEBFLOW)).toBe(WEBFLOW[6]);
    expect(closestDemographicOption("eeoRace", "Asian", GUSTO_RACE)).toBeNull();
  });
  it("a decline is no option that answers Yes or No", () => {
    // "Prefer not to say" took "Yes, I have a disability but do not want to provide specifics".
    expect(closestDemographicOption("eeoDisability", "Prefer not to say", HOOTSUITE)).toBe("Prefer not to answer");
    expect(declineOption(HOOTSUITE)).toBe("Prefer not to answer");
  });
});

/**
 * Rippling's pronoun list (live, round 8): blank for "She/Her" and "He/Him" on
 * all three postings once the widget picked by the self-ID rules, as two
 * options hold "she her". A stated set is the option of that set alone.
 */
describe("a pronoun list in the widget (Rippling)", () => {
  const RIPPLING_PRONOUNS = ["She/her/hers", "He/him/his", "They/them/theirs", "She/her/hers, they/them/theirs", "He/him/his, they/them/theirs", "Just use my name"];
  it("the combobox engine picks the stated set", async () => {
    const { input, shown } = reactSelect(RIPPLING_PRONOUNS);
    const res = await fillAriaCombobox(input, "She/Her", { ...fast, demographic: "eeoPronouns" });
    expect(res.filled).toBe(true);
    expect(shown()).toBe("She/her/hers");
  });
  it("the page-world driver picks it too", () => {
    expect(pickOption(RIPPLING_PRONOUNS, "He/Him", undefined, false, "eeoPronouns")).toBe(1);
  });
});
