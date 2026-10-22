// chrome-extension/src/content/adapters/janestreet.ts
/**
 * Jane Street's own application form (www.janestreet.com/join-jane-street/
 * apply/…, a Greenhouse posting). Its pronouns are clickable options
 * (`.pronouns-option[data-pronouns]`) above a text input: a click toggles an
 * option's "checked" class, and the page writes the checked options into the
 * input only at submit (its formatPronounsSubmission). Typed into, the input
 * checked the widget's "self-describe" with "She/Her" in it (live, round 8).
 * Stated pronouns an option names are that option, clicked as a person does;
 * any others are typed, as before.
 */
import { activateElement } from "../comboboxEngine";
import { ADAPTERS } from "./registry";
import type { AdapterFillResult, FillContext, SiteAdapter } from "./types";

const words = (s: string): string => s.toLowerCase().replace(/[^a-z]+/g, " ").trim();

const optionsOf = (input: HTMLElement): HTMLElement[] =>
  Array.from(input.parentElement?.querySelectorAll<HTMLElement>(".pronouns-option[data-pronouns]") ?? []);

/** The option for stated pronouns ("She/Her" is "she/her/hers"), or null. */
function pronounOption(input: HTMLInputElement, value: string): HTMLElement | null {
  const want = words(value);
  if (!want) return null;
  const hits = optionsOf(input).filter((o) => {
    const p = words(o.dataset.pronouns ?? "");
    return p !== "" && (p === want || p.startsWith(`${want} `));
  });
  return hits.length === 1 ? hits[0] : null;
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

async function clickPronouns(option: HTMLElement): Promise<AdapterFillResult> {
  if (!option.classList.contains("checked")) {
    activateElement(option.querySelector<HTMLElement>(".pronouns-clickable") ?? option);
    for (let i = 0; i < 10 && !option.classList.contains("checked"); i++) await sleep(50);
  }
  // A click the page took no notice of: never typed instead, which checks
  // the self-describe box with the answer in it.
  return option.classList.contains("checked") ? { filled: true } : { filled: false, reason: "The pronoun option did not take the click" };
}

export const janeStreetAdapter: SiteAdapter = {
  id: "janestreet",
  // The application page only: a match with recognized fields mounts the
  // panel, and the rest of the site is no application.
  match: (host, url) => /(^|\.)janestreet\.com$/i.test(host) && /\/join-jane-street\/apply\//i.test(url),

  fillOperation(ctx: FillContext): Promise<AdapterFillResult> | undefined {
    const el = ctx.el;
    if (!(el instanceof HTMLInputElement)) return undefined;
    const option = pronounOption(el, ctx.value);
    return option ? clickPronouns(option) : undefined;
  },

  // The checked options, as the page submits them ("she/her/hers or
  // they/them/theirs"): read from the input, which stays empty until submit,
  // the fill's read-back called the pick cleared and asked again (live,
  // round 8).
  currentValue(el: HTMLElement): string | undefined {
    if (!(el instanceof HTMLInputElement)) return undefined;
    const picked = optionsOf(el)
      .filter((o) => o.classList.contains("checked"))
      .map((o) => (o.dataset.pronouns ?? "").trim())
      .filter(Boolean);
    return picked.length ? picked.join(" or ") : undefined;
  },
};

ADAPTERS.push(janeStreetAdapter);
