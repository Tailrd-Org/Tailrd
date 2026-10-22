/**
 * A multi-select's items picked one at a time through the page-world driver.
 *
 * Greenhouse's remix react-select multi-select opens for no scripted event
 * (Gallup's "Please indicate which languages you speak…", live, round 7): the
 * combobox engine's typing leaves it shut, so no chip is ever added. The
 * driver picks each item from the widget's own options instead. An item the
 * widget already shows is skipped: react-select's selectOption toggles a
 * selected option off, so picking it again would remove it.
 */
import type { DriverResult } from "./mainWorldClient";
import { ANSWER_LIST_SEPARATOR } from "./answerKind";

export interface EachItemResult {
  /** Items the widget holds now: picked here, or already shown. */
  added: string[];
  /** The options the driver saw for an item it could not match. */
  options?: string[];
}

export async function driveEachItem(
  value: string,
  drive: (item: string) => Promise<DriverResult>,
  shown: () => string[]
): Promise<EachItemResult> {
  const items = [...new Set(value.split(ANSWER_LIST_SEPARATOR).map((s) => s.trim()).filter(Boolean))];
  const key = (s: string): string => s.toLowerCase().replace(/\s+/g, " ").trim();
  const added: string[] = [];
  let options: string[] | undefined;
  for (const item of items) {
    if (shown().some((s) => key(s) === key(item))) {
      added.push(item);
      continue;
    }
    const r = await drive(item);
    if (r.ok) added.push(item);
    else if (r.options?.length && !options) options = r.options;
  }
  return options ? { added, options } : { added };
}

/** The chips a react-select multi-select shows ("select__multi-value__label"). */
export function reactSelectChips(el: HTMLElement): string[] {
  const shell = el.closest('.select-shell, [class*="-container"], [class*="__container"]') ?? el.parentElement;
  if (!shell) return [];
  return Array.from(shell.querySelectorAll('[class*="multi-value__label"], [class*="multiValue__label"]'))
    .map((c) => (c.textContent ?? "").trim())
    .filter(Boolean);
}
