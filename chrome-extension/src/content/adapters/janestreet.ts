// chrome-extension/src/content/adapters/janestreet.ts
/**
 * Jane Street's own application form (www.janestreet.com/join-jane-street/
 * apply/…, a Greenhouse posting). Its pronouns are clickable options
 * (`.pronouns-option[data-pronouns]`) above a text input that keeps the
 * answer; typed into, the input checked the widget's "self-describe" with
 * "She/Her" in it (live, round 8). Stated pronouns an option names are that
 * option, clicked as a person does; any others are typed, as before.
 */
import { activateElement } from "../comboboxEngine";
import { ADAPTERS } from "./registry";
import { setNativeValue } from "./shared";
import type { AdapterFillResult, FillContext, SiteAdapter } from "./types";

const words = (s: string): string => s.toLowerCase().replace(/[^a-z]+/g, " ").trim();

/** The option for stated pronouns ("She/Her" is "she/her/hers"), or null. */
function pronounOption(input: HTMLInputElement, value: string): HTMLElement | null {
  const want = words(value);
  if (!want || !input.parentElement) return null;
  const hits = Array.from(input.parentElement.querySelectorAll<HTMLElement>(".pronouns-option[data-pronouns]")).filter((o) => {
    const p = words(o.dataset.pronouns ?? "");
    return p !== "" && (p === want || p.startsWith(`${want} `));
  });
  return hits.length === 1 ? hits[0] : null;
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

async function clickPronouns(input: HTMLInputElement, option: HTMLElement, value: string): Promise<AdapterFillResult> {
  if (!option.classList.contains("checked")) {
    activateElement(option.querySelector<HTMLElement>(".pronouns-clickable") ?? option);
    for (let i = 0; i < 10 && !option.classList.contains("checked"); i++) await sleep(50);
  }
  if (option.classList.contains("checked")) return { filled: true };
  // The page took no click: the answer typed, as the generic fill did.
  setNativeValue(input, value);
  return { filled: true };
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
    return option ? clickPronouns(el, option, ctx.value) : undefined;
  },
};

ADAPTERS.push(janeStreetAdapter);
