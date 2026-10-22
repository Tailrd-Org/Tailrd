// chrome-extension/test/janeStreetAdapter.test.ts
/**
 * Jane Street's own application form (www.janestreet.com/join-jane-street/
 * apply/…, a Greenhouse posting; live, round 8): its pronouns are clickable
 * options above a text input. A click toggles an option's "checked" class
 * and nothing else; the page writes the checked options into the input only
 * at submit (its formatPronounsSubmission). Typed into, the input checked the
 * widget's "self-describe" with "She/Her" in it. Markup as the page renders
 * it (the run's DOM capture, GET only).
 */
import { describe, it, expect, beforeEach, beforeAll, afterAll } from "vitest";
import { janeStreetAdapter } from "../src/content/adapters/janestreet";
import type { FillContext } from "../src/content/adapters/types";
import { scanPage, type RuntimeControl } from "../src/content/formScanner";
import * as P from "./e2e/profiles.mjs";
import type { UserApplicationProfile } from "../src/shared/types";
import { stubLayout } from "./helpers/layout";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());
beforeEach(() => {
  document.body.innerHTML = "";
});

const OPTION = (cls: string, value: string, text: string, extra = ""): string =>
  `<div class="pronouns-option ${cls}" data-pronouns="${value}"><div class="checkbox pronouns-clickable${extra}"><div class="checkmark"></div></div><span class="label-text pronouns-clickable${extra}">${text}</span></div>`;

/** The widget, and (when `live`) the page's handler: a click toggles the
 *  option's "checked" class, as Jane Street's does. */
function mount(live: boolean): HTMLInputElement {
  document.body.innerHTML = `<form id="application-form"><div class="form-field"><div id="pronouns-options-list" class="form-field"><label class="form-field-label" id="pronouns-label" for="pronouns">Pronouns <small>(Select one or more.)</small></label>${OPTION("she-her-hers", "she/her/hers", "she/her/hers")}${OPTION("he-him-his", "he/him/his", "he/him/his")}${OPTION("they-them-theirs", "they/them/theirs", "they/them/theirs")}${OPTION("", "", "self-describe, please specify:", " self-describe")}</div><input type="text" id="pronouns" name="pronouns" class="pronouns-input" tabindex="-1" maxlength="1000"></div></form>`;
  const input = document.getElementById("pronouns") as HTMLInputElement;
  if (live) {
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(".pronouns-clickable"))) {
      el.addEventListener("click", () => el.closest<HTMLElement>(".pronouns-option")!.classList.toggle("checked"));
    }
  }
  return input;
}

const fill = (el: HTMLElement, value: string): FillContext => ({ el, value, control: { el } as unknown as RuntimeControl });
const checked = (): string[] => Array.from(document.querySelectorAll<HTMLElement>(".pronouns-option.checked")).map((o) => o.dataset.pronouns ?? "");

describe("janeStreetAdapter", () => {
  it("matches Jane Street's application page only", () => {
    expect(janeStreetAdapter.match("www.janestreet.com", "https://www.janestreet.com/join-jane-street/apply/8599917002/?gh_jid=8599917002")).toBe(true);
    // The rest of the site is no application: a match would mount the panel.
    expect(janeStreetAdapter.match("www.janestreet.com", "https://www.janestreet.com/join-jane-street/open-roles/")).toBe(false);
    expect(janeStreetAdapter.match("janestreet.com.evil.com", "https://janestreet.com.evil.com/join-jane-street/apply/1/")).toBe(false);
    expect(janeStreetAdapter.match("job-boards.greenhouse.io", "")).toBe(false);
  });

  it("stated pronouns are their option, clicked: never the self-describe box", async () => {
    const input = mount(true);
    const op = janeStreetAdapter.fillOperation!(fill(input, "She/Her"));
    expect(op).toBeDefined();
    expect(await op).toEqual({ filled: true });
    expect(checked()).toEqual(["she/her/hers"]);
    // The page fills its input only at submit.
    expect(input.value).toBe("");
  });

  it("an option already checked is left checked: a second fill never unticks it", async () => {
    const input = mount(true);
    await janeStreetAdapter.fillOperation!(fill(input, "She/Her"));
    await janeStreetAdapter.fillOperation!(fill(input, "She/Her"));
    expect(checked()).toEqual(["she/her/hers"]);
  });

  it("the field holds the checked options, as the page submits them", async () => {
    const input = mount(true);
    expect(janeStreetAdapter.currentValue!(input)).toBeUndefined();
    await janeStreetAdapter.fillOperation!(fill(input, "She/Her"));
    expect(janeStreetAdapter.currentValue!(input)).toBe("she/her/hers");
    (document.querySelector(".they-them-theirs .pronouns-clickable") as HTMLElement).click();
    expect(janeStreetAdapter.currentValue!(input)).toBe("she/her/hers or they/them/theirs");
    // The scan reads it so: the fill's read-back called it cleared and asked again (live, round 8).
    const f = scanPage(P.UK_SENIOR as unknown as UserApplicationProfile, true, janeStreetAdapter).fields.find((x) => x.label.startsWith("Pronouns"));
    expect(f?.currentValue).toBe("she/her/hers or they/them/theirs");
  });

  it("pronouns no option names are typed, as the generic fill does", () => {
    const input = mount(true);
    expect(janeStreetAdapter.fillOperation!(fill(input, "Xe/Xem"))).toBeUndefined();
    expect(janeStreetAdapter.fillOperation!(fill(input, "She/They"))).toBeUndefined();
  });

  it("a click the page ignores is a failed fill, never the answer typed into the self-describe box", async () => {
    const input = mount(false);
    const res = await janeStreetAdapter.fillOperation!(fill(input, "He/Him"));
    expect(res?.filled).toBe(false);
    expect(input.value).toBe("");
  });

  it("any other field is the generic fill's", () => {
    document.body.innerHTML = `<input type="text" id="first_name" name="first_name">`;
    expect(janeStreetAdapter.fillOperation!(fill(document.getElementById("first_name")!, "Siobhan"))).toBeUndefined();
    expect(janeStreetAdapter.currentValue!(document.getElementById("first_name")!)).toBeUndefined();
  });
});
