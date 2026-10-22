import { describe, it, expect, vi } from "vitest";
import { shouldAdoptFrame, shouldAdoptRemoteHost, makeProxyCallbacks, dispatchFormOp } from "../src/content/crossFrame";
import type { OverlayCallbacks } from "../src/content/overlay";

describe("shouldAdoptRemoteHost", () => {
  it("adopts a child host only when the top frame has no recognized fields", () => {
    expect(shouldAdoptRemoteHost(0, 5)).toBe(true);
    expect(shouldAdoptRemoteHost(3, 5)).toBe(false); // top owns its own form → keep local
    expect(shouldAdoptRemoteHost(0, 0)).toBe(false); // remote has nothing either
  });
});

describe("shouldAdoptFrame", () => {
  it("a panel shown for the posting's own Apply button adopts the form its Apply opens in a frame (Comeet, live, round 8)", () => {
    // The top frame mounted for "Apply for this job" with no form of its own;
    // the form then opened in comeet.co's frame, and was never adopted.
    expect(shouldAdoptFrame({ adopted: false, panelShown: true }, 0, 14)).toBe(true);
    expect(shouldAdoptFrame({ adopted: false, panelShown: false }, 0, 14)).toBe(true);
  });
  it("never a second frame, nor beside a form of the top's own", () => {
    expect(shouldAdoptFrame({ adopted: true, panelShown: true }, 0, 14)).toBe(false);
    expect(shouldAdoptFrame({ adopted: false, panelShown: true }, 3, 14)).toBe(false);
  });
});

describe("makeProxyCallbacks", () => {
  it("marshals onAutofill through the transport and unwraps the value", async () => {
    const send = vi.fn(async () => ({ ok: true, value: { ok: 2, fail: 0, total: 2 } }));
    const cb = makeProxyCallbacks(send);
    const res = await cb.onAutofill(["a", "b"]);
    expect(send).toHaveBeenCalledWith("onAutofill", [["a", "b"]]);
    expect(res).toEqual({ ok: 2, fail: 0, total: 2 });
  });

  it("fires void methods (onProfileResolved) through the transport without throwing", async () => {
    const send = vi.fn(async () => ({ ok: true }));
    const cb = makeProxyCallbacks(send);
    cb.onProfileResolved(null);
    await Promise.resolve();
    expect(send).toHaveBeenCalledWith("onProfileResolved", [null]);
  });

  it("rejects a non-value op when the host reports failure", async () => {
    const send = vi.fn(async () => ({ ok: false, error: "frame gone" }));
    const cb = makeProxyCallbacks(send);
    await expect(cb.onInsertCoverLetter("x")).rejects.toThrow(/frame gone/);
  });

  it("marshals the gap-option harvest and returns its value", async () => {
    const seen: { op: string; args: unknown[] }[] = [];
    const proxy = makeProxyCallbacks(async (op, args) => {
      seen.push({ op, args });
      return { ok: true, value: { f1: ["Yes", "No"] } };
    });
    await expect(proxy.onHarvestGapOptions(["f1"])).resolves.toEqual({ f1: ["Yes", "No"] });
    expect(seen[0].op).toBe("onHarvestGapOptions");
    expect(seen[0].args).toEqual([["f1"]]);
  });

  // Was missing from ALL_OPS: in a cross-origin form frame the modal's Save &
  // fill called an undefined method and threw before writing anything.
  it("marshals the gap answers back to the form frame", async () => {
    const send = vi.fn(async () => ({ ok: true, value: { ok: true, filled: 1 } }));
    const proxy = makeProxyCallbacks(send);
    const answers = [{ gap: { fieldId: "f1" }, value: "Yes" }] as Parameters<
      OverlayCallbacks["onAnswerGaps"]
    >[0];
    await expect(proxy.onAnswerGaps(answers)).resolves.toEqual({ ok: true, filled: 1 });
    expect(send).toHaveBeenCalledWith("onAnswerGaps", [answers]);
  });
});

describe("dispatchFormOp", () => {
  it("invokes the named callback with the args and wraps the result", async () => {
    const onInsertCoverLetter = vi.fn(async () => ({ ok: true }));
    const ops = { onInsertCoverLetter } as unknown as OverlayCallbacks;
    const res = await dispatchFormOp(ops, "onInsertCoverLetter", ["hi"]);
    expect(onInsertCoverLetter).toHaveBeenCalledWith("hi");
    expect(res).toEqual({ ok: true, value: { ok: true } });
  });

  it("wraps a thrown error as ok:false", async () => {
    const ops = { onRescan: () => { throw new Error("boom"); } } as unknown as OverlayCallbacks;
    const res = await dispatchFormOp(ops, "onRescan", []);
    expect(res.ok).toBe(false);
    expect(res.error).toMatch(/boom/);
  });
});
