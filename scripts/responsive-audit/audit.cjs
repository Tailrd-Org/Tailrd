/* eslint-disable no-console */
/**
 * Responsive / overflow audit.
 *
 *   node scripts/responsive-audit/audit.cjs              # full matrix
 *   node scripts/responsive-audit/audit.cjs --state app-profile
 *   node scripts/responsive-audit/audit.cjs --viewport 390x844
 *   node scripts/responsive-audit/audit.cjs --shots      # screenshot every cell
 *
 * Renders every screen of the app at every viewport in the matrix with the API
 * stubbed (see fixtures.cjs), then runs the in-page detectors (detectors.cjs)
 * and writes report.json + report.md. Findings are mechanical: an element is
 * off the viewport, clipped by an overflow:hidden ancestor, or unreachable
 * inside a fixed layer. No judgement calls.
 *
 * Requires the Vite dev server on :5173 (the script starts one if it is down).
 */
const path = require("path");
const fs = require("fs");
const http = require("http");
const { spawn } = require("child_process");

const REPO = path.resolve(__dirname, "..", "..");
const { chromium } = require(path.join(REPO, "chrome-extension", "node_modules", "playwright"));
const F = require("./fixtures.cjs");
const { STATES, VIEWPORTS } = require("./states.cjs");
const { pageAudit } = require("./detectors.cjs");

const BASE = process.env.AUDIT_BASE || "http://localhost:5173";
const OUT = path.join(__dirname, "out");
const SHOTS = path.join(OUT, "shots");
const CONCURRENCY = Number(process.env.AUDIT_WORKERS || 4);

const argv = process.argv.slice(2);
const arg = (name) => {
  const i = argv.indexOf("--" + name);
  return i >= 0 ? argv[i + 1] : null;
};
const flag = (name) => argv.includes("--" + name);

/* ------------------------------------------------------------------ server */

function ping(url) {
  return new Promise((res) => {
    const req = http.get(url, (r) => {
      r.resume();
      res(r.statusCode < 500);
    });
    req.on("error", () => res(false));
    req.setTimeout(1500, () => {
      req.destroy();
      res(false);
    });
  });
}

async function ensureServer() {
  if (await ping(BASE)) {
    console.log("· dev server already up at " + BASE);
    return null;
  }
  console.log("· starting vite dev server…");
  const proc = spawn(process.platform === "win32" ? "npm.cmd" : "npm", ["run", "dev"], {
    cwd: path.join(REPO, "frontend"),
    stdio: "ignore",
    shell: process.platform === "win32",
  });
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    if (await ping(BASE)) {
      console.log("· dev server up");
      return proc;
    }
  }
  throw new Error("vite dev server did not come up on " + BASE);
}

/* ------------------------------------------------------------------- stubs */

const json = (body, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

function stubFor(pathname, method, userOverrides, empty) {
  const p = pathname;
  const user = { ...F.user, ...(userOverrides || {}) };

  if (p === "/auth/me") return json(user);
  if (p === "/auth/refresh") return json({ access_token: "stub" });
  if (p === "/auth/login" || p === "/auth/register" || p === "/auth/google")
    return json({ access_token: "stub" });
  if (p === "/auth/logout") return json({ ok: true });
  if (p === "/auth/sessions") return json({ sessions: F.sessions });
  if (p.startsWith("/auth/sessions")) return json({ ok: true });
  if (p === "/auth/me/onboarding" || p === "/auth/me/setup") return json(user);
  if (p === "/auth/verify-email" || p === "/auth/resend-verification") return json({ ok: true });
  if (p === "/auth/extension/authorize") return json({ code: "stubcode" });

  if (p === "/jobs/stats") return json(F.stats);
  if (p === "/jobs/sidebar") return json(F.sidebar);
  // A state can ask for the collection to come back empty: the empty state is the
  // first screen every new user sees, so it gets audited like any other screen.
  if (p === "/jobs/applications") return json(empty ? [] : F.applications);
  if (p === "/jobs") return json(F.jobs);
  if (/^\/jobs\/\d+\/fetch-details$/.test(p)) return json(F.jobs[0]);
  if (/^\/jobs\/\d+\/mark-applied$/.test(p)) return json({ ok: true });
  if (/^\/jobs\/\d+$/.test(p)) return json(F.jobs[0]);

  if (p === "/resumes") return method === "GET" ? json(F.resumes) : json(F.resumeDetail);
  if (p === "/resumes/upload") return json(F.resumeDetail);
  if (/^\/resumes\/\d+\/analyze$/.test(p)) return json(F.analysisReport);
  if (/^\/resumes\/\d+\/improve$/.test(p))
    return json({ profile: F.profile, changes: ["Quantified 3 bullets", "Rewrote the summary"] });
  if (/^\/resumes\/\d+\/primary$/.test(p)) return json({ ok: true });
  if (/^\/resumes\/\d+$/.test(p)) return json(F.resumeDetail);

  if (p === "/settings") return json(F.settings);
  if (p === "/settings/resume") return json({ ok: true });

  if (p === "/api/user/application-profile") return json(F.applicationProfile);
  if (p === "/api/custom-resume") return json(F.customResume);
  if (p === "/api/custom-resume-analysis") return json(F.customResumeAnalysis);
  if (p === "/api/cover-letter") return json(F.coverLetter);
  if (p === "/api/render-resume" || p === "/api/render-cover-letter") return json({ url: "" });

  // The AI modals post to /ai/<flow>/<jobId>. Specific flows before the catch-all.
  if (/^\/ai\/match-breakdown\//.test(p)) return json(F.matchBreakdown);
  if (/^\/ai\/custom-resume-analysis\//.test(p)) return json(F.customResumeAnalysis);
  if (/^\/ai\/custom-resume\//.test(p)) return json(F.customResume);
  if (/^\/ai\/cover-letter\//.test(p)) return json(F.coverLetter);
  if (p === "/ai/resume-versions") return json({ versions: [] });
  if (p.startsWith("/ai/")) return json({ status: "done", result: {} });

  if (p === "/feedback") return json(F.feedback);
  if (/^\/feedback\/\d+$/.test(p)) return json({ status: "deleted", id: 1 });

  if (p === "/github-sources") return json(F.githubSources);
  if (p.startsWith("/github-sources")) return json({ ok: true });

  if (p.startsWith("/connections")) return json({ connections: [], emails: [] });
  if (p.startsWith("/apply")) return json({ session_id: "s1", status: "done", progress: 100 });
  if (p === "/feedback") return json({ ok: true });
  if (p === "/health") return json({ ok: true });

  return null;
}

const API_PREFIX =
  /^\/(auth|api|jobs|resumes|settings|ai|apply|connections|github-sources|feedback|health)\b/;

/* ------------------------------------------------------------------ runner */

async function runState(browser, state, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.w, height: viewport.h },
    deviceScaleFactor: 1,
    hasTouch: viewport.touch,
    isMobile: false, // keep layout viewport == visual viewport so measurements are exact
  });

  await context.route("**/*", async (route) => {
    const req = route.request();
    let url;
    try {
      url = new URL(req.url());
    } catch {
      return route.continue();
    }
    // third-party: let fonts/icons through (they change metrics), kill the rest
    if (url.origin !== BASE) {
      if (/fonts\.(googleapis|gstatic)\.com|cdnjs\.cloudflare\.com/.test(url.host)) {
        return route.continue();
      }
      return route.fulfill({ status: 204, body: "" });
    }
    if (API_PREFIX.test(url.pathname)) {
      const stub = stubFor(url.pathname, req.method(), state.user, state.empty);
      if (stub) return route.fulfill(stub);
      return route.fulfill(json({}, 200));
    }
    return route.continue();
  });

  if (state.authed) {
    await context.addInitScript(() => {
      localStorage.setItem("access_token", "audit-stub-token");
    });
  }

  // Stand in for the extension content script that frames /embed/*: the page
  // posts {type:"ready"} with a MessagePort, we answer with {type:"init"}.
  // Loaded top-level, window.parent === window, so the message comes right back.
  if (state.embedBridge) {
    await context.addInitScript(() => {
      window.addEventListener("message", (e) => {
        if (e.data && e.data.type === "ready" && e.ports && e.ports[0]) {
          e.ports[0].postMessage({
            type: "init",
            token: "audit-stub-token",
            job: {
              title:
                "Senior Software Development Engineer II, Distributed Systems & Platform Infrastructure",
              company: "Hoffman-Rutherford Global Technology Solutions International",
              description:
                "We are looking for a talented engineer to join our platform team. " +
                "Responsibilities include designing and shipping distributed services.",
              url: "https://boards.greenhouse.io/averylongcompanyslug/jobs/1234567890",
            },
          });
        }
      });
    });
  }

  const page = await context.newPage();
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e.message).slice(0, 200)));

  const cell = { state: state.id, url: state.url, viewport: viewport.id, findings: [], errors: [] };

  try {
    await page.goto(BASE + state.url, { waitUntil: "domcontentloaded", timeout: 30000 });
    if (state.wait) {
      await page.waitForSelector(state.wait, { timeout: 8000 }).catch(() => {});
    }
    await page.waitForTimeout(500);

    for (const step of state.steps || []) {
      // Width-dependent chrome: the phone drawer's hamburger does not exist on
      // desktop, and the sidebar nav item it reveals is not clickable on a
      // phone. A step that is a no-op at some widths must not be a hard failure
      // there, but a step that SHOULD have worked and didn't must still be
      // loud, or the audit silently measures the wrong screen and calls it clean.
      if (step.clickIfVisible) {
        const el = page.locator(step.clickIfVisible).first();
        if (await el.isVisible().catch(() => false)) {
          await el.click({ timeout: 5000 }).catch(() => {});
          await page.waitForTimeout(300);
        }
      }
      if (step.click) {
        const el = page.locator(step.click).nth(step.nth || 0);
        await el.click({ timeout: 5000 }).catch((e) => cell.errors.push("click " + step.click + ": " + e.message.split("\n")[0]));
      }
      if (step.fill) {
        await page
          .locator(step.fill)
          .nth(step.nth || 0)
          .fill(step.value, { timeout: 5000 })
          .catch((e) => cell.errors.push("fill " + step.fill + ": " + e.message.split("\n")[0]));
      }
      if (step.selectIndex !== undefined) {
        await page
          .locator(step.selectIndex)
          .first()
          .selectOption({ index: step.index }, { timeout: 5000 })
          .catch((e) => cell.errors.push("select " + step.selectIndex + ": " + e.message.split("\n")[0]));
      }
      if (step.waitFor) {
        // If the thing we were supposed to open never opened, we are about to
        // audit a completely different screen and report it clean. That is worse
        // than a missed defect. It is a false all-clear. Fail loudly.
        const reached = await page
          .waitForSelector(step.waitFor, { timeout: 6000 })
          .then(() => true)
          .catch(() => false);
        if (!reached) {
          cell.findings.push({
            type: "state-not-reached",
            severity: "high",
            selector: step.waitFor,
            chain: step.waitFor,
            text: "",
            detail:
              "the screen never appeared (waited for `" + step.waitFor + "`). This cell audited " +
              "whatever was underneath instead, so treat any 'clean' result for it as meaningless",
            overflowPx: 0,
          });
        }
      }
      await page.waitForTimeout(350);
    }

    // let fonts settle, web fonts change every measurement
    await page.evaluate(() => document.fonts && document.fonts.ready).catch(() => {});
    await page.waitForTimeout(250);

    const findings = await page.evaluate(
      ({ src, opts }) => {
        // eslint-disable-next-line no-new-func
        const fn = new Function("return (" + src + ")")();
        return fn(opts);
      },
      { src: pageAudit.toString(), opts: { touch: viewport.touch } }
    );

    // push, NOT assign. The steps loop above pushes `state-not-reached` findings
    // into cell.findings; an assignment here silently discarded every one of them
    // before any consumer (the counts, the report) could see it, so the loud form
    // was never loud, and a state that never rendered still reported clean. That is
    // the precise false all-clear the waitFor step exists to prevent.
    cell.findings.push(...findings);
    cell.errors.push(...consoleErrors);

    const shouldShoot = flag("shots") || cell.findings.some((f) => f.severity === "high");
    if (shouldShoot) {
      fs.mkdirSync(SHOTS, { recursive: true });
      await page
        .screenshot({
          path: path.join(SHOTS, `${state.id}__${viewport.id}.png`),
          fullPage: true,
          animations: "disabled",
        })
        .catch(() => {});
    }
  } catch (e) {
    cell.errors.push("NAV: " + String(e.message).split("\n")[0]);
  } finally {
    await context.close();
  }
  return cell;
}

async function main() {
  const server = await ensureServer();
  fs.mkdirSync(OUT, { recursive: true });

  const stateFilter = arg("state");
  const vpFilter = arg("viewport");
  const states = stateFilter ? STATES.filter((s) => s.id.includes(stateFilter)) : STATES;
  const viewports = vpFilter ? VIEWPORTS.filter((v) => v.id === vpFilter) : VIEWPORTS;

  const jobs = [];
  for (const s of states) {
    for (const v of viewports) {
      // Some screens only exist at some widths (the phone drawer, the desktop
      // collapse toggle). Auditing them where they cannot be reached is noise.
      if (s.minWidth && v.w < s.minWidth) continue;
      if (s.maxWidth && v.w > s.maxWidth) continue;
      jobs.push([s, v]);
    }
  }
  console.log(`· ${states.length} screens × ${viewports.length} viewports = ${jobs.length} cells\n`);

  const browser = await chromium.launch();
  const cells = [];
  let done = 0;

  async function worker() {
    while (jobs.length) {
      const [s, v] = jobs.shift();
      const cell = await runState(browser, s, v);
      cells.push(cell);
      done++;
      const high = cell.findings.filter((f) => f.severity === "high").length;
      const med = cell.findings.filter((f) => f.severity === "medium").length;
      const low = cell.findings.filter((f) => f.severity === "low").length;
      const mark = high ? "✗" : med ? "!" : "·";
      process.stdout.write(
        `${mark} [${String(done).padStart(3)}/${cells.length + jobs.length}] ${s.id} @ ${v.id}` +
          (high || med || low ? `  ${high}H ${med}M ${low}L` : "  clean") +
          (cell.errors.length ? `  ⚠ ${cell.errors.length} err` : "") +
          "\n"
      );
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, worker));
  await browser.close();
  if (server) server.kill();

  // --out lets several audits run side by side without clobbering each other.
  const tag = arg("out") || "report";
  cells.sort((a, b) => a.state.localeCompare(b.state) || a.viewport.localeCompare(b.viewport));
  fs.writeFileSync(path.join(OUT, tag + ".json"), JSON.stringify({ cells }, null, 2));

  writeMarkdown(cells, tag);

  const totals = { high: 0, medium: 0, low: 0 };
  for (const c of cells) for (const f of c.findings) totals[f.severity]++;
  console.log(
    `\n=== ${totals.high} high · ${totals.medium} medium · ${totals.low} low ===\n` +
      `report: scripts/responsive-audit/out/${tag}.md`
  );
  process.exitCode = totals.high > 0 ? 1 : 0;
}

/** Group by (state, finding signature) so one broken rule reads as one row. */
function writeMarkdown(cells, tag) {
  const byState = new Map();
  for (const c of cells) {
    if (!byState.has(c.state)) byState.set(c.state, []);
    byState.get(c.state).push(c);
  }

  const lines = ["# Responsive audit", "", `Generated ${new Date().toISOString()}`, ""];
  const sevRank = { high: 0, medium: 1, low: 2 };

  const summary = [];
  for (const [state, cs] of byState) {
    const n = cs.reduce((a, c) => a + c.findings.filter((f) => f.severity !== "low").length, 0);
    summary.push([state, n]);
  }
  summary.sort((a, b) => b[1] - a[1]);
  lines.push("| screen | high+medium findings |", "| --- | --- |");
  for (const [s, n] of summary) lines.push(`| ${s} | ${n || ", "} |`);
  lines.push("");

  for (const [state, cs] of byState) {
    const groups = new Map();
    for (const c of cs) {
      for (const f of c.findings) {
        const key = f.type + "|" + f.chain + "|" + f.text;
        if (!groups.has(key)) groups.set(key, { f, viewports: [], maxPx: 0 });
        const g = groups.get(key);
        g.viewports.push(c.viewport);
        g.maxPx = Math.max(g.maxPx, f.overflowPx || 0);
      }
    }
    if (!groups.size) continue;
    const sorted = [...groups.values()].sort(
      (a, b) => sevRank[a.f.severity] - sevRank[b.f.severity] || b.maxPx - a.maxPx
    );
    lines.push(`## ${state}`, "");
    for (const g of sorted) {
      lines.push(
        `- **[${g.f.severity}] ${g.f.type}**: \`${g.f.chain}\`` +
          (g.f.text ? `\n  - content: “${g.f.text}”` : "") +
          `\n  - ${g.f.detail}` +
          `\n  - viewports: ${g.viewports.join(", ")}`
      );
    }
    lines.push("");
  }
  fs.writeFileSync(path.join(OUT, tag + ".md"), lines.join("\n"));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
