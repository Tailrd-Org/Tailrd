/**
 * Applies adapter hooks over the generic pipeline. Each helper falls back to
 * generic behavior when the adapter is null, the hook is absent, the hook
 * declines (undefined), or the hook throws, so an adapter can only ever refine,
 * never break, the pipeline.
 */
import { classifyField, resolveProfileValue, type Classification } from "../fieldMatcher";
import { applyOverride } from "../overrides";
import type { FieldCategory, ResolveControl, UserApplicationProfile } from "../../shared/types";
import type { RuntimeControl } from "../formScanner";
import type { AdapterFillResult, FieldContext, FillContext, SiteAdapter } from "./types";

function safe<T>(fn: () => T, label: string): T | undefined {
  try {
    return fn();
  } catch (e) {
    console.warn(`[adapter ${label}]`, e);
    return undefined;
  }
}

export function classifyWithAdapter(adapter: SiteAdapter | null, ctx: FieldContext): Classification {
  const generic = classifyField(ctx.signals);
  const adapted = adapter?.classify
    ? (safe(() => adapter.classify!(ctx, generic), "classify") ?? generic)
    : generic;
  // Server-side override wins last: it's the hot-fix for when generic/adapter
  // are wrong. No-op when no rules are loaded.
  return applyOverride(ctx, adapted);
}

export function resolveAnswerWithAdapter(
  adapter: SiteAdapter | null,
  category: FieldCategory,
  profile: UserApplicationProfile | null,
  control: ResolveControl,
  fillEEO: boolean,
  el: HTMLElement
): string | null {
  if (!profile) return null;
  if (adapter?.resolveAnswer) {
    const override = safe(() => adapter.resolveAnswer!({ category, profile, control, fillEEO, el }), "resolveAnswer");
    if (override !== undefined) return override;
  }
  return resolveProfileValue(category, profile, control, fillEEO);
}

/** undefined = adapter declines this field (generic fill); Promise = adapter owns it. */
export function tryAdapterOperation(
  adapter: SiteAdapter | null,
  ctx: FillContext
): Promise<AdapterFillResult> | undefined {
  if (!adapter?.fillOperation) return undefined;
  return safe(() => adapter.fillOperation!(ctx), "fillOperation");
}

/** Give the adapter first refusal on each item; run claimed ops, return the rest. */
export async function runAdapterOperations(
  adapter: SiteAdapter | null,
  items: { fieldId: string; value: string }[],
  getControl: (id: string) => RuntimeControl | undefined
): Promise<{ opOutcomes: { fieldId: string; ok: boolean; reason?: string }[]; remaining: { fieldId: string; value: string }[] }> {
  const opOutcomes: { fieldId: string; ok: boolean; reason?: string }[] = [];
  const remaining: { fieldId: string; value: string }[] = [];
  for (const it of items) {
    const control = getControl(it.fieldId);
    const op = control?.el ? tryAdapterOperation(adapter, { control, value: it.value, el: control.el }) : undefined;
    if (op) {
      const r: AdapterFillResult = await op.catch(() => ({ filled: false as const }));
      // The adapter's reason is what the report says about a field it could
      // not fill (Lever's location search listing nothing in time).
      opOutcomes.push({ fieldId: it.fieldId, ok: r.filled, reason: r.reason });
    } else {
      remaining.push(it);
    }
  }
  return { opOutcomes, remaining };
}
