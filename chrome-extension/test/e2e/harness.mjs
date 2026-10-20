/**
 * Real-extension e2e harness.
 *
 * Loads the BUILT extension (dist/) into Chromium exactly as a user installs it
 * (--load-extension + --disable-extensions-except), points it at the local fake
 * backend (fakeApi.mjs), opens a page, presses the panel's own Autofill button,
 * waits for the fill to finish, and dumps every field's final value and type
 * from the page DOM.
 *
 * Pages are served at their REAL ATS URLs by intercepting the navigation
 * (context.route → fulfill), so the content script sees the real hostname and
 * the site adapters match exactly as they would in production.
 *
 * Network posture (safety): in "fixture" mode every request that is not a
 * fixture or the fake API is aborted, the run is fully offline. In "live" mode
 * only safe reads are allowed out: GET/HEAD, plus GraphQL POSTs whose operation
 * is a `query` (Ashby renders its form from those). Every other non-GET request
 * (form submits, uploads, mutations, autosave, beacons) is aborted, so a live
 * run can never submit or transmit an application.
 *
 * Headful by default (HEADLESS=1 opts into --headless=new). On Windows no xvfb
 * is needed: the window simply opens.
 */
import { chromium } from "playwright";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import { fakeJwt } from "./fakeApi.mjs";
import { dumpAllFrames } from "./dumpFields.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
// E2E_EXT_DIR scores another build (e.g. a baseline commit's dist) against the
// current cases and expectations.
export const EXT_DIR = process.env.E2E_EXT_DIR
  ? path.resolve(process.env.E2E_EXT_DIR)
  : path.resolve(here, "..", "..", "dist");

export async function launchExtension({ headless = process.env.HEADLESS === "1" } = {}) {
  const userDataDir = mkdtempSync(path.join(os.tmpdir(), "tailrd-e2e-"));
  const args = [
    `--disable-extensions-except=${EXT_DIR}`,
    `--load-extension=${EXT_DIR}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1400,1000",
  ];
  if (headless) args.push("--headless=new");
  const ctx = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    args,
    viewport: { width: 1366, height: 900 },
  });
  let sw = ctx.serviceWorkers()[0];
  if (!sw) sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 });
  return {
    ctx,
    sw,
    async close() {
      await ctx.close().catch(() => {});
      try {
        rmSync(userDataDir, { recursive: true, force: true });
      } catch {
        // Windows keeps a lock on the profile for a moment; the tmp dir is disposable.
      }
    },
  };
}

/** Point the extension at the fake API and sign it in with a harness token. */
export async function seedExtension(sw, apiUrl, { fillEEO = true } = {}) {
  const { token, exp } = fakeJwt();
  await sw.evaluate(
    async ({ apiUrl, token, exp, fillEEO }) => {
      await chrome.storage.local.clear();
      await chrome.storage.local.set({
        // Pages wait for a press: the harness reads each filled page, and a page
        // that turned by itself mid-read would score the wrong one.
        ap_config: { apiBaseUrl: apiUrl, dashboardUrl: apiUrl, useMockData: false, fillEEO, flowAutoContinue: false },
        ap_auth: { refreshToken: "harness-refresh", email: "harness@example.com" },
      });
      await chrome.storage.session.set({ ap_auth_access: token, ap_auth_access_exp: exp });
    },
    { apiUrl, token, exp, fillEEO }
  );
}

function isSafeLiveRequest(req) {
  const method = req.method();
  if (method === "GET" || method === "HEAD" || method === "OPTIONS") return true;
  if (method !== "POST") return false;
  const body = req.postData() || "";
  try {
    const parsed = JSON.parse(body);
    const ops = Array.isArray(parsed) ? parsed : [parsed];
    return ops.every((op) => typeof op?.query === "string" && /^\s*(query\b|\{)/.test(op.query));
  } catch {
    return false;
  }
}

/**
 * Install the routing for one run. `pages` maps an exact URL (no hash) to the
 * HTML to serve for it. Returns a counter of what was blocked, for the report.
 */
export async function installRouting(ctx, { apiUrl, pages, mode, assets = new Map(), allowRequests = [] }) {
  // A case may name read-only endpoints its page needs to keep working (an
  // email-validation lookup): never a submission, an upload or an account.
  const allowed = allowRequests.map((re) => new RegExp(re));
  const blocked = [];
  await ctx.unrouteAll({ behavior: "ignoreErrors" }).catch(() => {});
  await ctx.route("**/*", async (route) => {
    const req = route.request();
    const url = req.url().split("#")[0];
    if (url.startsWith(apiUrl)) return route.continue();
    const html = pages.get(url);
    if (html !== undefined && req.method() === "GET") {
      return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: html });
    }
    const asset = assets.get(url);
    if (asset && req.method() === "GET") {
      return route.fulfill({ status: 200, contentType: asset.contentType, body: asset.body });
    }
    if (mode === "live" && (isSafeLiveRequest(req) || allowed.some((re) => re.test(url)))) return route.continue();
    if (mode === "live") blocked.push(`${req.method()} ${url.slice(0, 120)}`);
    return route.abort();
  });
  return blocked;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * The flow is waiting for the user, or has ended: a parked beat ("paused…",
 * "Done.…", a Next-page gate), or any last beat that is not a "Step N" one,
 * since a stop names its own reason ("No application form found on this
 * page"). Matching only "Autofill flow stopped" made every such stop look like
 * a hang that waited out the whole timeout (round 5 batch C).
 */
const PARKED = /\b(paused|done\.|review and submit|review this page|then next page|waiting for|stopped)\b/i;
const flowParked = (beat) => PARKED.test(beat) || (beat !== "" && !/^Step \d/.test(beat));

async function overlayState(page) {
  return page
    .evaluate(() => {
      const sr = document.getElementById("applypilot-overlay-host")?.shadowRoot;
      const btn = sr?.querySelector("#ap-btn-autofill");
      const banner = sr?.querySelector(".ap-banner");
      return {
        host: Boolean(document.getElementById("applypilot-overlay-host")),
        button: Boolean(btn),
        enabled: Boolean(btn && !btn.disabled),
        text: btn?.textContent?.trim() ?? "",
        banner: banner && banner.style.display !== "none" ? (banner.textContent || "").trim() : "",
      };
    })
    .catch(() => ({ host: false, button: false, enabled: false, text: "", banner: "" }));
}

async function togglePanel(sw, pageUrl) {
  return sw.evaluate(async (url) => {
    const tabs = await chrome.tabs.query({});
    const tab = tabs.find((t) => (t.url || "").split("#")[0] === url.split("#")[0]) ?? tabs[tabs.length - 1];
    if (!tab?.id) return false;
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "TOGGLE_PANEL" }, { frameId: 0 });
      return true;
    } catch {
      return false;
    }
  }, pageUrl);
}

/**
 * Open the panel (auto-mount or toolbar toggle), press Autofill, and wait for
 * the fill to finish. Returns a trace of what happened for the report.
 */
export async function triggerAutofill(page, sw, api, { mountWaitMs = 8000, fillTimeoutMs = 150000, clickImmediately = false } = {}) {
  const trace = { mounted: "none", clicked: false, finished: false, telemetry: null, ms: 0 };
  const started = Date.now();
  let st = await overlayState(page);
  for (let waited = 0; !st.button && waited < mountWaitMs; waited += 250) {
    await sleep(250);
    st = await overlayState(page);
  }
  if (st.button) trace.mounted = "auto";
  for (let i = 0; !st.button && i < 30; i++) {
    await togglePanel(sw, page.url());
    await sleep(500);
    st = await overlayState(page);
    if (st.button) trace.mounted = "toggle";
  }
  if (!st.button) return { ...trace, error: "panel never mounted" };

  for (let waited = 0; !st.enabled && waited < 30000; waited += 250) {
    await sleep(250);
    st = await overlayState(page);
  }
  if (!st.enabled) return { ...trace, error: `Autofill button never enabled (text=${st.text})` };

  // The button is live as soon as the profile loads, but on an SPA (Ashby,
  // SmartRecruiters) the form renders later; a click before the panel has any
  // field selected is a silent no-op. Wait for the panel to report selected
  // fields, unchanged for a second (its own refreshMainView console beat).
  // clickImmediately: press it the moment it is enabled, as a user who opened
  // the form a second ago would.
  const panel = page.__panel ?? { selected: 0, fields: 0, changedAt: Date.now(), beats: [] };
  for (let waited = 0; !clickImmediately && waited < 25000; waited += 200) {
    if (panel.selected > 0 && Date.now() - panel.changedAt > 1000) break;
    // A posting whose form opens behind an "Apply" button has no fields
    // until the flow clicks it: after 8 s of nothing, press Autofill anyway.
    if (panel.fields === 0 && waited >= 8000) break;
    await sleep(200);
  }
  trace.selectedAtClick = panel.selected;
  trace.fieldsAtClick = panel.fields;

  const telemetryBefore = api.state.telemetry.length;
  const beatsBefore = panel.beats.length;
  await page.locator("#ap-btn-autofill").click({ timeout: 10000 });
  trace.clicked = true;

  // Busy → "Working…"; the click handler resolves when fillOnce has finished
  // its whole pass (telemetry is sent at the very end of fillOnce). A fill that
  // throws resets the button within one poll and shows "Autofill failed: …" in
  // the banner, so "never saw busy" for a few seconds is an answer too, not a
  // reason to sit out the whole timeout.
  // The first pass can be a zero-field page whose flow then clicks "Apply" and
  // fills the NEXT page in the background, so "the button went idle" is not
  // the end. The flow's own beats are: "Step N · filling…", "opening …",
  // then a parked one ("paused: …", "Done. …", a Next-page gate) that means
  // it is waiting for the user, which is when the page is final.
  const deadline = Date.now() + fillTimeoutMs;
  const clickedAt = Date.now();
  let sawBusy = false;
  let idleSince = 0;
  while (Date.now() < deadline) {
    st = await overlayState(page);
    if (/working/i.test(st.text)) sawBusy = true;
    if (/autofill failed/i.test(st.banner)) {
      trace.error = `extension: ${st.banner}`;
      break;
    }
    const newBeats = panel.beats.slice(beatsBefore);
    const lastBeat = newBeats[newBeats.length - 1] ?? "";
    const parked = flowParked(lastBeat);
    const busy = /working/i.test(st.text);
    if (!busy) idleSince = idleSince || Date.now();
    else idleSince = 0;
    if (parked && !busy && Date.now() - idleSince > 500) break;
    // No flow beat at all (older builds / a page with nothing to do): idle for 3 s.
    if (sawBusy && !busy && newBeats.length === 0 && Date.now() - idleSince > 3000) break;
    if (!sawBusy && newBeats.length === 0 && Date.now() - clickedAt > 8000) {
      trace.error = `fill never started (button "${st.text}", banner "${st.banner}")`;
      break;
    }
    await sleep(150);
  }
  if (api.state.telemetry.length > telemetryBefore) {
    trace.telemetry = api.state.telemetry[api.state.telemetry.length - 1].body;
  }
  trace.beats = panel.beats.slice(beatsBefore);
  trace.banner = st.banner;
  // Let late commits / framework re-renders settle before reading the page.
  await sleep(1500);
  if (!trace.telemetry && api.state.telemetry.length > telemetryBefore) {
    trace.telemetry = api.state.telemetry[telemetryBefore].body;
  }
  trace.finished = Date.now() < deadline;
  trace.ms = Date.now() - started;
  return trace;
}

/**
 * Press the panel's Next page gate, as an applicant reading each page would,
 * and wait for the flow to park on the page it opens. Null when there is no
 * gate to press. It risks nothing more than page 1 did: the flow never clicks
 * Submit, and every non-GET request stays blocked, so a Next that would save
 * to the server leaves the page where it was.
 */
export async function continueToNextPage(page, api, { timeoutMs = 120000 } = {}) {
  const panel = page.__panel;
  const gate = page.locator("#ap-flow-next");
  if (!(await gate.isVisible().catch(() => false))) return null;
  const beatsBefore = panel.beats.length;
  const telemetryBefore = api.state.telemetry.length;
  const urlBefore = page.url();
  await gate.click({ timeout: 10000 });
  const deadline = Date.now() + timeoutMs;
  let parked = false;
  while (Date.now() < deadline) {
    const newBeats = panel.beats.slice(beatsBefore);
    const st = await overlayState(page);
    if (flowParked(newBeats[newBeats.length - 1] ?? "") && !/working/i.test(st.text)) {
      parked = true;
      break;
    }
    await sleep(250);
  }
  await sleep(1500);
  return {
    beats: panel.beats.slice(beatsBefore),
    telemetry: api.state.telemetry.length > telemetryBefore ? api.state.telemetry[api.state.telemetry.length - 1].body : null,
    urlBefore,
    url: page.url(),
    parked,
  };
}

/** Run one case end to end. */
export async function runCase(env, testCase) {
  const { ctx, sw, api } = env;
  if (testCase.profile) api.setProfile(testCase.profile);
  // Résumés the sync advertises (an uploadable file makes the flow attach it).
  api.setResumes(testCase.resumes ?? []);
  const pages = new Map(Object.entries(testCase.pages ?? {}).map(([u, h]) => [u.split("#")[0], h]));
  if (testCase.html !== undefined) pages.set(testCase.url.split("#")[0], testCase.html);
  const assets = new Map(Object.entries(testCase.assets ?? {}));
  const blocked = await installRouting(ctx, { apiUrl: api.url, pages, mode: testCase.mode ?? "fixture", assets, allowRequests: testCase.allowRequests });
  const page = await ctx.newPage();
  // A slow machine, for a page whose late rendering is the point
  // (SmartRecruiters' web components draw their labels after the page is
  // up): Chromium's CPU throttling, for the whole case.
  if (testCase.cpuThrottle) {
    const cdp = await ctx.newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: testCase.cpuThrottle });
  }
  const consoleLines = [];
  const panel = { selected: 0, fields: 0, changedAt: Date.now(), beats: [] };
  page.__panel = panel;
  page.on("console", (m) => {
    const t = m.text();
    // Every flow beat (the overlay's progress line is the only bare
    // "[Tailrd] " log): a stop names its own reason.
    if (/^\[Tailrd\] /.test(t)) panel.beats.push(t.slice(9, 200));
    const beat = /refreshMainView selected=\s*(\d+)\s+of fields=\s*(\d+)/.exec(t);
    if (beat) {
      const selected = Number(beat[1]);
      const fields = Number(beat[2]);
      if (selected !== panel.selected || fields !== panel.fields) {
        panel.selected = selected;
        panel.fields = fields;
        panel.changedAt = Date.now();
      }
    }
    if (/\[Tailrd|\[adapter|\[combobox/i.test(t) && !/refreshMainView/.test(t)) consoleLines.push(t.slice(0, 300));
    else if (m.type() === "error" && /chrome-extension:|contentScript|Tailrd|applypilot/i.test(`${t} ${m.location()?.url ?? ""}`)) {
      consoleLines.push(`ERROR ${t.slice(0, 400)}`);
    }
  });
  page.on("pageerror", (err) => consoleLines.push(`PAGEERROR ${String(err?.stack || err).slice(0, 400)}`));
  try {
    // Every case starts from a clean site: ATSes persist drafts (Workable
    // restores the previous applicant's answers from site storage, which the
    // extension then rightly refuses to overwrite).
    try {
      const cdp = await ctx.newCDPSession(page);
      await cdp.send("Storage.clearDataForOrigin", { origin: new URL(testCase.url).origin, storageTypes: "all" });
      await cdp.detach();
    } catch {
      // older Chromium / non-http URL: nothing to clear
    }
    // Domain-wide cookies too: two employers on *.bamboohr.com share them, and
    // the second case behaved differently after the first.
    await ctx.clearCookies().catch(() => {});
    await page.goto(testCase.url, { waitUntil: "load", timeout: 60000 });
    if (testCase.beforeFill) await testCase.beforeFill(page);
    await sleep(testCase.settleMs ?? 800);
    const before = await dumpAllFrames(page);
    const trace = await triggerAutofill(page, sw, api, testCase.trigger ?? {});
    // Blur everything and let the framework re-render: a value the framework
    // never registered is reset to its (empty) state on this render, which is
    // the "appears, then clears on blur" failure the state check exists for.
    if (testCase.stateSelector) {
      await page.evaluate(() => (document.activeElement instanceof HTMLElement ? document.activeElement.blur() : undefined)).catch(() => {});
      await sleep(600);
    }
    const after = await dumpAllFrames(page);
    // Debugging hook: inspect the filled page (a widget's real DOM) before it closes.
    if (testCase.afterFill) await testCase.afterFill(page, consoleLines);
    let state = null;
    if (testCase.stateSelector) {
      state = await page
        .evaluate((sel) => {
          const el = document.querySelector(sel);
          try {
            return el ? JSON.parse(el.textContent || "null") : null;
          } catch {
            return null;
          }
        }, testCase.stateSelector)
        .catch(() => null);
    }
    let screenshot = null;
    if (testCase.screenshotPath) {
      await page.screenshot({ path: testCase.screenshotPath, fullPage: true }).catch(() => {});
      screenshot = testCase.screenshotPath;
    }
    // A multi-page form: press the panel's Next page gate up to `nextPages`
    // times and keep what each page held after its fill. Pins (`expect`)
    // still read page 1; review.cjs shows every page.
    const nextPages = [];
    for (let i = 0; i < (testCase.nextPages ?? 0); i++) {
      const step = await continueToNextPage(page, api, { timeoutMs: testCase.trigger?.fillTimeoutMs ?? 120000 });
      if (!step) break;
      step.after = await dumpAllFrames(page);
      nextPages.push(step);
      const last = step.beats[step.beats.length - 1] ?? "";
      if (/\b(done\.|review and submit|stopped)\b/i.test(last) || (last !== "" && !/^Step \d/.test(last))) break;
    }
    return { before, after, trace, blocked: [...blocked], console: consoleLines, screenshot, state, nextPages };
  } finally {
    await page.close().catch(() => {});
  }
}
