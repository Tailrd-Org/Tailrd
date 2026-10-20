/**
 * The pinned regression suite, in batches: every case that has expectations,
 * `--size` per batch (default 13: one Chromium, and much more than ~15 live
 * pages in one run exhausts memory on this machine). Each batch is saved as
 * results/<prefix>-<n>.json (+ .txt), then the failing cases are listed.
 *
 *   node build.mjs && node test/e2e/tools/regress.mjs [--prefix regress] [--only <id substring>] [--size 13]
 *
 * Headful unless HEADLESS=1 (Ashby renders nothing headless). The whole
 * suite takes about 2.5 hours. A page that misses one field once is often a
 * flake (Rippling, Workable): re-run it alone before calling it a regression.
 * After editing pins, re-score a saved run with rescore.mjs instead of
 * re-running the browser; read failures with fails.cjs, changes with rundiff.cjs.
 */
import { readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const e2e = path.resolve(here, "..");
const ext = path.resolve(e2e, "..", "..");
const argv = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : fallback;
};
const prefix = opt("--prefix", "regress");
const only = opt("--only", "");
const size = Number(opt("--size", "13"));

const ids = [];
// SmartRecruiters' bot check lets one page through per fresh browser on this
// machine (round 7): each of its cases runs in a batch of its own.
const alone = [];
for (const f of readdirSync(path.join(e2e, "cases")).filter((f) => f.endsWith(".mjs")).sort()) {
  const mod = await import(pathToFileURL(path.join(e2e, "cases", f)).href);
  const list = typeof mod.default === "function" ? await mod.default() : mod.default;
  for (const c of list ?? []) {
    if (Object.keys(c.expect ?? {}).length === 0) continue; // exploration cases pin nothing
    if (only && !c.id.includes(only)) continue;
    (c.ats === "smartrecruiters" ? alone : ids).push(c.id);
  }
}
if (ids.length + alone.length === 0) {
  console.log("no pinned cases matched");
  process.exit(1);
}

const batches = [];
for (let i = 0; i < ids.length; i += size) batches.push(ids.slice(i, i + size));
for (const id of alone) batches.push([id]);
ids.push(...alone);
console.log(`${ids.length} pinned case(s) in ${batches.length} batch(es) of up to ${size}`);
mkdirSync(path.join(e2e, "results"), { recursive: true });

const failing = [];
batches.forEach((batch, n) => {
  const name = `${prefix}-${n + 1}`;
  const run = spawnSync(
    process.execPath,
    [path.join(e2e, "run.mjs"), "--filter", batch.map((id) => `=${id}`).join(","), "--report-only", "--save", name],
    { cwd: ext, encoding: "utf8", maxBuffer: 256 * 1024 * 1024 }
  );
  const out = `${run.stdout ?? ""}${run.stderr ?? ""}`;
  writeFileSync(path.join(e2e, "results", `${name}.txt`), out);
  const pass = (out.match(/^PASS /gm) ?? []).length;
  const fail = (out.match(/^FAIL\s+\S+/gm) ?? []).map((l) => l.split(/\s+/)[1]);
  failing.push(...fail);
  console.log(`batch ${n + 1}/${batches.length} (${name}): ${pass} pass, ${fail.length} fail`);
});

if (failing.length) {
  console.log(`\nFAILING (read: node test/e2e/tools/fails.cjs test/e2e/results/<batch>.json; re-run alone to rule out a flake):`);
  for (const id of failing) console.log(`  ${id}`);
  process.exit(1);
}
console.log("\nall pinned cases pass");
