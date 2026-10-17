/**
 * REGRESSION (live iCIMS, 2026-10-17): the panel opens on the posting, whose
 * Apply opens the candidate login INSIDE the posting's frame. That frame then
 * owns the form and the panel adopts it, but the panel had loaded the profile
 * already and never gave it to the frame: the frame's fields had no values and
 * its flow never resumed. An adopted frame gets the profile the panel holds.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { OverlayCallbacks } from "../src/content/overlay";

function stubChrome(): void {
  const sendMessage = vi.fn(async (msg: { type: string }) => {
    if (msg.type === "GET_STATUS") return { mode: "connected" };
    if (msg.type === "GET_PROFILE") return { ok: true, profile: { firstName: "Maya", lastName: "Tremblay", email: "maya@example.com" } };
    return { ok: true };
  });
  (globalThis as unknown as { chrome: unknown }).chrome = {
    runtime: { id: "test", sendMessage, getURL: (p: string) => p },
    storage: {
      local: { get: vi.fn(async () => ({})), set: vi.fn(async () => {}), remove: vi.fn(async () => {}) },
      onChanged: { addListener: vi.fn(), removeListener: vi.fn() },
    },
  };
}

function callbacks(onProfileResolved: OverlayCallbacks["onProfileResolved"]): OverlayCallbacks {
  const named: Partial<OverlayCallbacks> = {
    onAutofill: async () => ({ ok: 0, fail: 0, total: 0 }),
    onListResumes: async () => [],
    onProfileResolved,
  };
  return new Proxy(named, {
    get: (t, k: string) => (k in t ? t[k as keyof OverlayCallbacks] : async () => ({ ok: true })),
  }) as OverlayCallbacks;
}

beforeEach(() => {
  document.body.innerHTML = "";
  stubChrome();
  vi.resetModules();
});
afterEach(() => {
  delete (globalThis as { chrome?: unknown }).chrome;
});

describe("a form frame adopted after the profile loaded", () => {
  it("gets the profile the panel holds (iCIMS: the login opens in the posting's frame)", async () => {
    const { showOverlay } = await import("../src/content/overlay");
    const top = vi.fn();
    showOverlay({ fields: [], tabUrl: "https://careers-x.icims.com/jobs/1/job", applyEntry: "Apply for this job online" }, callbacks(top));
    await vi.waitFor(() => expect(top).toHaveBeenCalled());

    const frame = vi.fn();
    showOverlay({ fields: [], tabUrl: "https://careers-x.icims.com/jobs/1/login" }, callbacks(frame));
    await vi.waitFor(() => expect(frame).toHaveBeenCalledTimes(1));
    expect(frame.mock.calls[0][0]).toMatchObject({ email: "maya@example.com" });
  });

  it("does not feed the same callbacks again when the panel is shown again", async () => {
    const { showOverlay } = await import("../src/content/overlay");
    const top = vi.fn();
    const cb = callbacks(top);
    showOverlay({ fields: [], tabUrl: "https://careers-x.icims.com/jobs/1/job" }, cb);
    await vi.waitFor(() => expect(top).toHaveBeenCalled());
    const calls = top.mock.calls.length;
    showOverlay({ fields: [], tabUrl: "https://careers-x.icims.com/jobs/1/job" }, cb);
    await new Promise((r) => setTimeout(r, 50));
    expect(top.mock.calls.length).toBe(calls);
  });
});
