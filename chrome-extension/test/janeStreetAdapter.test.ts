// chrome-extension/test/janeStreetAdapter.test.ts
/**
 * Jane Street's own application form (www.janestreet.com/join-jane-street/
 * apply/…, a Greenhouse posting; live, round 8): its pronouns are clickable
 * options above a text input that keeps the answer. Typed into, the input
 * checked the widget's "self-describe" with "She/Her" in it. Markup as the
 * page renders it (the run's DOM capture, GET only).
 */
import { describe, it, expect, beforeEach } from "vitest";
import { janeStreetAdapter } from "../src/content/adapters/janestreet";
import type { FillContext } from "../src/content/adapters/types";
import type { RuntimeControl } from "../src/content/formScanner";

beforeEach(() => {
  document.body.innerHTML = "";
});

const OPTION = (cls: string, value: string, text: string, extra = ""): string =>
  `<div class="pronouns-option ${cls}" data-pronouns="${value}"><div class="checkbox pronouns-clickable${extra}"><div class="checkmark"></div></div><span class="label-text pronouns-clickable${extra}">${text}</span></div>`;

/** The widget, and (when `live`) the page's handler: a click toggles the
 *  option and writes the checked options into the input. */
function mount(live: boolean): HTMLInputElement {
  document.body.innerHTML = `<div class="form-field"><div id="pronouns-options-list" class="form-field"><label class="form-field-label" id="pronouns-label" for="pronouns">Pronouns <small>(Select one or more.)</small></label>${OPTION("she-her-hers", "she/her/hers", "she/her/hers")}${OPTION("he-him-his", "he/him/his", "he/him/his")}${OPTION("they-them-theirs", "they/them/theirs", "they/them/theirs")}${OPTION("", "", "self-describe, please specify:", " self-describe")}</div><input type="text" id="pronouns" name="pronouns" class="pronouns-input" tabindex="-1" maxlength="1000"></div>`;
  const input = document.getElementById("pronouns") as HTMLInputElement;
  if (live) {
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(".pronouns-clickable"))) {
      el.addEventListener("click", () => {
        const option = el.closest<HTMLElement>(".pronouns-option")!;
        option.classList.toggle("checked");
        input.value = Array.from(document.querySelectorAll<HTMLElement>(".pronouns-option.checked"))
          .map((o) => o.dataset.pronouns)
          .filter(Boolean)
          .join(", ");
      });
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
    expect(input.value).toBe("she/her/hers");
  });

  it("pronouns no option names are typed, as the generic fill does", () => {
    const input = mount(true);
    expect(janeStreetAdapter.fillOperation!(fill(input, "Xe/Xem"))).toBeUndefined();
    expect(janeStreetAdapter.fillOperation!(fill(input, "She/They"))).toBeUndefined();
  });

  it("a click the page ignores falls back to typing the answer", async () => {
    const input = mount(false);
    expect(await janeStreetAdapter.fillOperation!(fill(input, "He/Him"))).toEqual({ filled: true });
    expect(input.value).toBe("He/Him");
  });

  it("any other field is the generic fill's", () => {
    document.body.innerHTML = `<input type="text" id="first_name" name="first_name">`;
    expect(janeStreetAdapter.fillOperation!(fill(document.getElementById("first_name")!, "Siobhan"))).toBeUndefined();
  });
});
