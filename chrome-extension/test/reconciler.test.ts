import { describe, it, expect, beforeEach } from "vitest";
import { AutofillReconciler } from "../src/content/reconciler";
import type { RuntimeControl } from "../src/content/formScanner";
import { forgetUserFocus } from "../src/content/userFocus";

/** Instant settle window so cycle logic is exercised without real waiting. */
const instant = async (): Promise<void> => {};

function reg(controls: RuntimeControl[]): Map<string, RuntimeControl> {
  return new Map(controls.map((c) => [c.id, c]));
}

function input(id: string, value = ""): { control: RuntimeControl; el: HTMLInputElement } {
  const el = document.createElement("input");
  el.type = "text";
  el.value = value;
  document.body.appendChild(el);
  return { control: { id, controlType: "text", el }, el };
}

/**
 * Dispatch an event that stands in for genuine user interaction. The reconciler
 * tells its own synthetic writes apart from the user by a write-in-progress
 * window, so any event we fire outside a reconciler write counts as the user.
 */
function userEvent(el: HTMLElement, type: string): void {
  el.dispatchEvent(new Event(type, { bubbles: true }));
}

beforeEach(() => {
  document.body.innerHTML = "";
  forgetUserFocus(document);
});

describe("AutofillReconciler, happy path", () => {
  it("fills mapped fields and marks them stable", async () => {
    const a = input("f-1");
    const b = input("f-2");
    const engine = new AutofillReconciler({ sleep: instant, observe: false });

    const reports = await engine.run(
      [
        { fieldId: "f-1", value: "Wissam" },
        { fieldId: "f-2", value: "Elmasry" },
      ],
      reg([a.control, b.control])
    );
    engine.dispose();

    expect(a.el.value).toBe("Wissam");
    expect(b.el.value).toBe("Elmasry");
    expect(reports.every((r) => r.status === "stable" && r.ok)).toBe(true);
  });
});

describe("AutofillReconciler, drift reconciliation", () => {
  it("re-applies a field the framework wipes during the settle window", async () => {
    const a = input("f-1");
    let wiped = false;
    const sleep = async (): Promise<void> => {
      if (!wiped) {
        wiped = true;
        a.el.value = ""; // framework overwrites the value after our first fill
      }
    };
    const engine = new AutofillReconciler({ sleep, observe: false });

    const reports = await engine.run([{ fieldId: "f-1", value: "Wissam" }], reg([a.control]));
    engine.dispose();

    expect(a.el.value).toBe("Wissam");
    expect(reports[0].status).toBe("stable");
    expect(reports[0].attempts).toBeGreaterThanOrEqual(2);
  });

  it("gives up after the cycle budget and reports drift honestly", async () => {
    const a = input("f-1");
    // A field whose value is wiped on every settle window, can never stabilize.
    const sleep = async (): Promise<void> => {
      a.el.value = "";
    };
    const engine = new AutofillReconciler({ sleep, observe: false, maxCycles: 3 });

    const reports = await engine.run([{ fieldId: "f-1", value: "Wissam" }], reg([a.control]));
    engine.dispose();

    expect(reports[0].ok).toBe(false);
    expect(reports[0].status).toBe("drifted");
  });
});

describe("AutofillReconciler, idempotency", () => {
  it("re-running an already-filled form writes nothing and stays stable", async () => {
    const a = input("f-1", "Wissam"); // already correct
    let writes = 0;
    a.el.addEventListener("input", () => writes++);
    const engine = new AutofillReconciler({ sleep: instant, observe: false });

    const reports = await engine.run([{ fieldId: "f-1", value: "Wissam" }], reg([a.control]));
    engine.dispose();

    expect(writes).toBe(0); // verify-before-write => no events fired
    expect(reports[0].status).toBe("stable");
    expect(a.el.value).toBe("Wissam");
  });
});

describe("AutofillReconciler, fills around CAPTCHA (never suspends the form)", () => {
  it("fills the normal fields even when a reCAPTCHA widget is on the page", async () => {
    // A reCAPTCHA widget present on the page must NOT stop the rest of the form
    // from filling. We skip the captcha itself and fill everything else.
    document.body.innerHTML = `<div class="g-recaptcha" data-sitekey="abc"></div>`;
    const a = input("f-1");
    const engine = new AutofillReconciler({ sleep: instant, observe: false });

    const reports = await engine.run([{ fieldId: "f-1", value: "Wissam" }], reg([a.control]));
    engine.dispose();

    expect(a.el.value).toBe("Wissam");
    expect(reports[0].status).toBe("stable");
  });
});

describe("AutofillReconciler, targeted reapply", () => {
  it("reapplies only the drifted field on reconcileNow", async () => {
    const a = input("f-1");
    const b = input("f-2");
    let bWrites = 0;
    b.el.addEventListener("input", () => bWrites++);
    const engine = new AutofillReconciler({ sleep: instant, observe: false });

    await engine.run(
      [
        { fieldId: "f-1", value: "A" },
        { fieldId: "f-2", value: "B" },
      ],
      reg([a.control, b.control])
    );
    expect(bWrites).toBe(1); // initial fill wrote b once

    a.el.value = ""; // framework wipes ONLY field A
    const reports = await engine.reconcileNow();
    engine.dispose();

    expect(a.el.value).toBe("A"); // reapplied
    expect(b.el.value).toBe("B"); // untouched
    expect(bWrites).toBe(1); // field B was NOT rewritten
    expect(reports.find((r) => r.fieldId === "f-1")?.status).toBe("stable");
  });
});

describe("AutofillReconciler, removed field", () => {
  it("reports a field removed from the DOM as not ok", async () => {
    const a = input("f-1");
    const engine = new AutofillReconciler({ sleep: instant, observe: false });
    a.el.remove();

    const reports = await engine.run([{ fieldId: "f-1", value: "X" }], reg([a.control]));
    engine.dispose();

    expect(reports[0].ok).toBe(false);
  });
});

describe("AutofillReconciler, observer-driven background reconciliation", () => {
  it("restores a wiped value after a DOM mutation while observing", async () => {
    const a = input("f-1");
    const engine = new AutofillReconciler({ sleep: instant, observerDebounceMs: 5 });

    await engine.run([{ fieldId: "f-1", value: "Z" }], reg([a.control]));
    expect(a.el.value).toBe("Z");

    a.el.value = ""; // framework wipes value (a bare property change MO can't see)
    document.body.appendChild(document.createElement("div")); // structural mutation MO DOES see

    await new Promise((r) => setTimeout(r, 40)); // let debounce + reconcile run
    engine.dispose();

    expect(a.el.value).toBe("Z"); // observer-driven reconciliation restored it
  });
});

describe("AutofillReconciler, stops churning on unfillable fields", () => {
  it("does not keep refilling a field it gave up on after the cycle budget", async () => {
    const a = input("f-1");
    // The value never sticks across the settle window, mirrors a custom
    // dropdown the text writer can't actually drive.
    const engine = new AutofillReconciler({
      sleep: async () => {
        a.el.value = "";
      },
      observe: false,
      maxCycles: 3,
    });

    const reports = await engine.run([{ fieldId: "f-1", value: "X" }], reg([a.control]));
    expect(reports[0].ok).toBe(false); // honestly reported as not filled

    // A later background pass must write NOTHING: refilling a field we can't
    // fill just steals its focus on every page mutation (the dropdown churn).
    let writes = 0;
    a.el.addEventListener("input", () => writes++);
    await engine.reconcileNow();
    engine.dispose();

    expect(writes).toBe(0);
  });
});

describe("AutofillReconciler, respects the user after autofill", () => {
  it("does not revert a field the user edits after it was filled", async () => {
    const a = input("f-1");
    const engine = new AutofillReconciler({ sleep: instant, observerDebounceMs: 5 });

    await engine.run([{ fieldId: "f-1", value: "Wissam" }], reg([a.control]));
    expect(a.el.value).toBe("Wissam");

    // User clicks into the field and types their own answer.
    userEvent(a.el, "pointerdown");
    a.el.value = "My own answer";
    userEvent(a.el, "input");

    // The page re-renders (a structural mutation the observer reacts to).
    document.body.appendChild(document.createElement("div"));
    await new Promise((r) => setTimeout(r, 40));
    engine.dispose();

    // Background drift correction must treat the user's edit as authoritative.
    expect(a.el.value).toBe("My own answer");
  });

  it("does not steal focus from a field the user is editing", async () => {
    const a = input("f-1", "Alpha"); // already correct → stays stable, never written
    const b = input("f-2");
    const engine = new AutofillReconciler({ sleep: instant, observerDebounceMs: 5 });

    await engine.run(
      [
        { fieldId: "f-1", value: "Alpha" },
        { fieldId: "f-2", value: "Beta" },
      ],
      reg([a.control, b.control])
    );
    expect(b.el.value).toBe("Beta");

    // User is actively editing field A: they clicked into it.
    userEvent(a.el, "pointerdown");
    a.el.focus();
    expect(document.activeElement).toBe(a.el);

    // Meanwhile the framework wipes field B and re-renders.
    b.el.value = "";
    document.body.appendChild(document.createElement("div"));
    await new Promise((r) => setTimeout(r, 40));
    engine.dispose();

    // Refilling B would focus()/blur() it and yank the caret out of A. The
    // reconciler must defer while the user is mid-edit.
    expect(document.activeElement).toBe(a.el);
  });
});

// iCIMS's login (Kinaxis, live 2026-10-17): its email box has `autofocus`,
// so on a full-page login it held focus from load. The guard took that for
// the user typing there and never wrote it; the page then rejected the empty
// email. Focus the page gave a box is not the user's.
describe("AutofillReconciler, focus the page gave a field", () => {
  it("fills a field the page focused before any user action", async () => {
    const a = input("f-1");
    a.el.focus(); // the page's autofocus, no click or key before it
    const engine = new AutofillReconciler({ sleep: instant, observe: false });
    const reports = await engine.run([{ fieldId: "f-1", value: "maya@example.com" }], reg([a.control]));
    engine.dispose();
    expect(a.el.value).toBe("maya@example.com");
    expect(reports[0].ok).toBe(true);
  });

  it("still leaves a field the user clicked into", async () => {
    const a = input("f-1");
    userEvent(a.el, "pointerdown");
    a.el.focus();
    const engine = new AutofillReconciler({ sleep: instant, observe: false });
    await engine.run([{ fieldId: "f-1", value: "maya@example.com" }], reg([a.control]));
    engine.dispose();
    expect(a.el.value).toBe("");
    expect(document.activeElement).toBe(a.el);
  });
});

describe("addTargets, merges without resetting existing tracking", () => {
  it("fills new targets and keeps prior fields in the engine state", async () => {
    document.body.innerHTML = `<input id="a" /><input id="b" />`;
    const a = document.getElementById("a") as HTMLInputElement;
    const b = document.getElementById("b") as HTMLInputElement;
    const registry = new Map<string, RuntimeControl>([
      ["a", { id: "a", controlType: "text", el: a }],
      ["b", { id: "b", controlType: "text", el: b }],
    ]);
    const engine = new AutofillReconciler({ sleep: async () => {}, settleWindowMs: 0, observe: false });

    const first = await engine.run([{ fieldId: "a", value: "alpha" }], registry);
    expect(first.find((r) => r.fieldId === "a")?.ok).toBe(true);

    const second = await engine.addTargets([{ fieldId: "b", value: "beta" }], registry);

    // Only the new target is reported back…
    expect(second.map((r) => r.fieldId)).toEqual(["b"]);
    expect(second[0].ok).toBe(true);
    expect(b.value).toBe("beta");
    // …and the original field is still filled (not wiped).
    expect(a.value).toBe("alpha");
  });
});

describe("AutofillReconciler, cancellation (Stop mid-fill)", () => {
  it("writes nothing when the signal is already aborted", async () => {
    const a = input("f-1");
    const controller = new AbortController();
    controller.abort();
    const engine = new AutofillReconciler({ sleep: instant, observe: false });

    const reports = await engine.run(
      [{ fieldId: "f-1", value: "Wissam" }],
      reg([a.control]),
      controller.signal
    );
    engine.dispose();

    expect(a.el.value).toBe(""); // never written, cancelled before any write
    expect(reports[0].ok).toBe(false);
  });

  it("stops cycling once aborted, even when a field never settles", async () => {
    const a = input("f-1");
    const controller = new AbortController();
    let sleeps = 0;
    // Wipe every settle window so the field can never stabilize; without the
    // abort this would run the full cycle budget (maxCycles sleeps).
    const sleep = async (): Promise<void> => {
      sleeps++;
      a.el.value = "";
      controller.abort(); // Stop pressed after the first cycle
    };
    const engine = new AutofillReconciler({ sleep, observe: false, maxCycles: 5 });

    await engine.run([{ fieldId: "f-1", value: "Wissam" }], reg([a.control]), controller.signal);
    engine.dispose();

    expect(sleeps).toBe(1); // aborted after cycle 0 → cycles 1..4 skipped
  });

  it("starts no background reconciliation for a cancelled fill", async () => {
    const a = input("f-1");
    const controller = new AbortController();
    controller.abort();
    const engine = new AutofillReconciler({ sleep: instant, observe: true });

    await engine.run([{ fieldId: "f-1", value: "Wissam" }], reg([a.control]), controller.signal);
    // A cancelled fill must NOT arm the drift-correction observer, or it would
    // resurrect fills the user just stopped.
    expect((engine as unknown as { observer: MutationObserver | null }).observer).toBeNull();
    engine.dispose();
  });
});
