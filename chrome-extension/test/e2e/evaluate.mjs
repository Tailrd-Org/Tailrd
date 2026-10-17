/**
 * Scores one case: the page's final field values (dumpFields) against the
 * case's expectations, plus a check the expectations cannot express on their
 * own: ANY field that changed and was not expected to is a failure. That is the
 * check that catches a wrong-kind write ("Quebec" typed into a yes/no question),
 * the worst outcome a filler can have, because it looks like success.
 *
 * Expectation values:
 *   "text"             normalized equality (case/whitespace/trailing-* insensitive)
 *   null               must be left blank (empty after the fill)
 *   { re: "..." }      case-insensitive regex
 *   { any: true }      any non-empty value
 *   { oneOf: [...] }   normalized equality with one of these
 *   { unchanged: true} must hold exactly what it held before the fill
 *   { today: "DD/MM/YYYY" } the run's date in that format (also MM/DD/YYYY,
 *                      YYYY-MM-DD): a pattern for "a date" let round 4 pin
 *                      10 May for 5 October
 *
 * Keys: the dump's own key ("#id", "name=x", "radio=x", "auto=x"), or
 * "label:<text>" to match the first field whose label contains <text>, or
 * "all:<type>~<text>" / "all:<text>" to match EVERY such field (a form that
 * repeats one label, e.g. an "I don't wish to answer" box per EEO question,
 * on a page whose ids change every load, like Ashby's).
 */
export function norm(v) {
  return String(v ?? "")
    .replace(/\s+/g, " ")
    .replace(/[\s*✱]+$/, "")
    .trim()
    .toLowerCase();
}

function findRecord(records, key) {
  if (key.startsWith("label:")) {
    const want = norm(key.slice(6));
    return records.find((r) => norm(r.label).includes(want)) ?? null;
  }
  // "<type>~<label text>": the field of that type whose label contains the text
  // (an ATS that renders a native radio AND an ARIA twin for one question).
  const typed = /^([a-z-]+)~(.+)$/.exec(key);
  if (typed) {
    const want = norm(typed[2]);
    return records.find((r) => r.type === typed[1] && norm(r.label).includes(want)) ?? null;
  }
  return records.find((r) => r.key === key) ?? null;
}

/** `at` (the run's own date, local time) written in `format`. */
function formatDay(format, at) {
  const p = (n) => String(n).padStart(2, "0");
  const [y, m, d] = [at.getFullYear(), p(at.getMonth() + 1), p(at.getDate())];
  if (format === "DD/MM/YYYY") return `${d}/${m}/${y}`;
  if (format === "MM/DD/YYYY") return `${m}/${d}/${y}`;
  return `${y}-${m}-${d}`;
}

function matches(expected, actual, beforeValue, runAt = new Date()) {
  if (expected === null) return norm(actual) === "";
  if (typeof expected === "string") return norm(actual) === norm(expected);
  if (expected.today) return norm(actual) === norm(formatDay(expected.today, runAt));
  if (expected.re) {
    try {
      return new RegExp(expected.re, "i").test(String(actual ?? ""));
    } catch {
      return false; // a broken expectation fails its row, never the whole run
    }
  }
  if (expected.any) return norm(actual) !== "";
  if (expected.oneOf) return expected.oneOf.some((o) => norm(o) === norm(actual));
  if (expected.unchanged) return String(actual ?? "") === String(beforeValue ?? "");
  return false;
}

function describe(expected) {
  if (expected === null) return "(blank)";
  if (typeof expected === "string") return JSON.stringify(expected);
  if (expected.re) return `/${expected.re}/`;
  if (expected.any) return "(any value)";
  if (expected.oneOf) return `oneOf ${JSON.stringify(expected.oneOf)}`;
  if (expected.unchanged) return "(unchanged)";
  if (expected.today) return `(today, ${expected.today})`;
  return JSON.stringify(expected);
}

/**
 * A cookie-consent manager's own controls (OneTrust's "#ot-group-id-C0003").
 * The extension never scans them (consent.ts), so a change there is the
 * page's: OneTrust renders its categories after the before-dump, ticked by
 * default (iCIMS DocuSign, live 2026-10-17).
 */
const CONSENT_MANAGER_KEY = /^#(ot-|onetrust|cookiebot|usercentrics|cookiescript)/i;

export function evaluateCase(testCase, result) {
  const rows = [];
  // A saved run is re-scored against the day it ran, not the day it is read.
  const runAt = result.at ? new Date(result.at) : new Date();
  const usedKeys = new Set();
  const beforeByKey = new Map(result.before.map((r) => [`${r.frame}|${r.key}`, r]));
  const expectations = testCase.expect ?? {};
  for (const [key, raw] of Object.entries(expectations)) {
    // { ifPresent: x }: a section the ATS renders only sometimes (Workable's
    // optional Education / Experience); checked when it is there.
    const optional = raw !== null && typeof raw === "object" && "ifPresent" in raw;
    const expected = optional ? raw.ifPresent : raw;
    if (key.startsWith("all:")) {
      const spec = key.slice(4);
      const typed = /^([a-z-]+)~(.+)$/.exec(spec);
      const want = norm(typed ? typed[2] : spec);
      const hits = result.after.filter((r) => (!typed || r.type === typed[1]) && norm(r.label).includes(want));
      if (hits.length === 0) {
        if (!optional) rows.push({ key, status: "MISSING", expected: describe(expected), actual: "", label: "" });
        continue;
      }
      hits.forEach((rec, i) => {
        usedKeys.add(`${rec.frame}|${rec.key}`);
        const before = beforeByKey.get(`${rec.frame}|${rec.key}`)?.value ?? "";
        const ok = matches(expected, rec.value, before, runAt);
        rows.push({ key: `${key}#${i + 1}`, status: ok ? "PASS" : "FAIL", kind: expected === null ? "abstain" : "fill", expected: describe(expected), actual: rec.value, label: rec.label, type: rec.type });
      });
      continue;
    }
    const rec = findRecord(result.after, key);
    if (!rec) {
      if (!optional) rows.push({ key, status: "MISSING", expected: describe(expected), actual: "", label: "" });
      continue;
    }
    usedKeys.add(`${rec.frame}|${rec.key}`);
    const before = beforeByKey.get(`${rec.frame}|${rec.key}`)?.value ?? "";
    const ok = matches(expected, rec.value, before, runAt);
    rows.push({
      key,
      status: ok ? "PASS" : "FAIL",
      kind: expected === null ? "abstain" : "fill",
      expected: describe(expected),
      actual: rec.value,
      label: rec.label,
      type: rec.type,
    });
  }
  // Unexpected writes: changed, and nobody said this field should change.
  const unexpected = [];
  for (const rec of result.after) {
    const id = `${rec.frame}|${rec.key}`;
    if (usedKeys.has(id)) continue;
    if (CONSENT_MANAGER_KEY.test(rec.key)) continue;
    const before = beforeByKey.get(id)?.value ?? "";
    if (String(rec.value ?? "") !== String(before)) {
      unexpected.push({ key: rec.key, label: rec.label, type: rec.type, before, actual: rec.value });
    }
  }
  // Framework state (React/Vue/Angular models exposed by the page): the value
  // must have REGISTERED with the framework, not merely sit in the DOM.
  for (const [key, expected] of Object.entries(testCase.expectState ?? {})) {
    const actual = result.state ? result.state[key] : undefined;
    const ok = result.state !== null && result.state !== undefined && matches(expected, actual ?? "", "");
    rows.push({
      key: `state.${key}`,
      status: result.state ? (ok ? "PASS" : "FAIL") : "MISSING",
      kind: expected === null ? "abstain" : "fill",
      expected: describe(expected),
      actual: actual ?? "",
      label: "framework state",
      type: "state",
    });
  }
  const pass = rows.filter((r) => r.status === "PASS").length;
  const total = rows.length + unexpected.length;
  return {
    id: testCase.id,
    ats: testCase.ats,
    rows,
    unexpected,
    pass,
    total,
    fills: rows.filter((r) => r.kind === "fill").length,
    fillsPassed: rows.filter((r) => r.kind === "fill" && r.status === "PASS").length,
    abstains: rows.filter((r) => r.kind === "abstain").length,
    abstainsPassed: rows.filter((r) => r.kind === "abstain" && r.status === "PASS").length,
    ok: pass === rows.length && unexpected.length === 0,
  };
}

export function formatCase(ev, trace) {
  const lines = [];
  const head = `${ev.ok ? "PASS" : "FAIL"}  ${ev.id}  [${ev.ats}]  ${ev.pass}/${ev.total}` +
    (trace?.error ? `  (trigger: ${trace.error})` : "") +
    (trace?.ms ? `  ${Math.round(trace.ms / 100) / 10}s` : "");
  lines.push(head);
  for (const r of ev.rows) {
    if (r.status === "PASS" && process.env.E2E_VERBOSE !== "1") continue;
    lines.push(
      `   ${r.status.padEnd(7)} ${r.key.padEnd(34).slice(0, 34)} exp=${r.expected.slice(0, 50)} got=${JSON.stringify(String(r.actual ?? "")).slice(0, 60)}${r.label ? `  «${r.label.slice(0, 50)}»` : ""}`
    );
  }
  for (const u of ev.unexpected) {
    lines.push(
      `   UNEXPECTED ${u.key.padEnd(31).slice(0, 31)} (${u.type}) ${JSON.stringify(String(u.before)).slice(0, 20)} -> ${JSON.stringify(String(u.actual)).slice(0, 60)}  «${(u.label || "").slice(0, 50)}»`
    );
  }
  return lines.join("\n");
}
