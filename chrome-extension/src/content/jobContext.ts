/**
 * Scrapes a job posting's description, title and company from the page so AI
 * answers (POST /api/fill) and the cover-letter generator (Feature B) have
 * context. Best-effort and failure-tolerant: returns empty strings rather than
 * throwing, because AI fill still works (lower quality) without context.
 */
import type { JobContext } from "../shared/types";

const MAX_DESC = 6000;
const MIN_DESC = 200;

const DESC_SELECTORS = [
  '[class*="job-description" i]',
  '[class*="jobdescription" i]',
  '[data-testid*="description" i]',
  '[id*="job-description" i]',
  '[class*="description" i]',
  "article",
  '[role="main"]',
  "main",
];

function visibleText(el: Element | null): string {
  if (!el) return "";
  return (el.textContent ?? "").replace(/\s+/g, " ").trim();
}

function extractDescription(doc: Document): string {
  for (const sel of DESC_SELECTORS) {
    const el = doc.querySelector(sel);
    const text = visibleText(el);
    if (text.length >= MIN_DESC) return text.slice(0, MAX_DESC);
  }
  // Fallback: the largest text block, ignoring chrome/navigation containers.
  let best = "";
  for (const el of Array.from(doc.querySelectorAll("section, article, div, p"))) {
    if (el.closest("nav, footer, header")) continue;
    if (el.querySelector("nav, footer")) continue; // skip wrappers that contain site chrome
    const text = visibleText(el);
    if (text.length > best.length) best = text;
  }
  return best.length >= MIN_DESC ? best.slice(0, MAX_DESC) : "";
}

function extractTitle(doc: Document): string {
  const h1 = visibleText(doc.querySelector("h1"));
  if (h1) return h1.slice(0, 200);
  const titled = visibleText(doc.querySelector('[class*="title" i]'));
  if (titled) return titled.slice(0, 200);
  return (doc.title || "").trim().slice(0, 200);
}

/** The JobPosting's hiringOrganization name from the page's JSON-LD, if any. */
function jsonLdCompany(doc: Document): string {
  for (const s of Array.from(doc.querySelectorAll('script[type="application/ld+json"]'))) {
    try {
      const data = JSON.parse(s.textContent ?? "");
      for (const item of Array.isArray(data) ? data : [data, ...(Array.isArray(data?.["@graph"]) ? data["@graph"] : [])]) {
        const org = item?.hiringOrganization;
        const name = typeof org === "string" ? org : org?.name;
        if (typeof name === "string" && name.trim()) return name.trim();
      }
    } catch {
      // Malformed JSON-LD names nobody.
    }
  }
  return "";
}

function extractCompany(doc: Document): string {
  const og = doc
    .querySelector('meta[property="og:site_name"]')
    ?.getAttribute("content");
  if (og && og.trim()) return og.trim().slice(0, 120);
  const ld = jsonLdCompany(doc);
  if (ld) return ld.slice(0, 120);
  // Greenhouse's board pages and embeds name the company only in the title,
  // "Job Application for <role> at <Company>" (Anduril, Planet, MongoDB's
  // embed; regression 2026-10-05: unknown, their own careers-site option was
  // no longer theirs).
  const titled = /^\s*job application for .+ at (.+?)\s*$/i.exec(doc.title || "");
  if (titled) return titled[1].slice(0, 120);
  // JazzHR's apply pages too: "<role> - <Company> - Career Page" (JazzHR bank,
  // round 8: the company was "brand", from its logo's "brand logo").
  const career = /^.+ - (.+?) - Career Page\s*$/i.exec(doc.title || "");
  if (career) return career[1].slice(0, 120);
  // A header logo's alt names it: Lever's <img alt="Palantir Technologies
  // logo"> (live 2026-10-08: unknown, "Palantir Website" was not the
  // company's own site and "Other" was chosen for a stated company website).
  const logoAlt = doc.querySelector('.main-header-logo img[alt], header img[alt$="logo" i], [class*="header" i] [class*="logo" i] img[alt$="logo" i]')?.getAttribute("alt") ?? "";
  const fromLogo = logoAlt.replace(/\s*\blogo\s*$/i, "").replace(/\s+/g, " ").trim();
  if (fromLogo && fromLogo.length <= 80 && !/^(company|home|careers?|jobs?|brand)$/i.test(fromLogo)) return fromLogo;
  // A name the page keeps for its header, shown or not (Paylocity's
  // #LayoutLogoName, hidden beside its logo, live 2026-10-05).
  const kept = (doc.querySelector('[id*="companyname" i], [id*="logoname" i], [class*="company-name" i], [class*="companyname" i]')?.textContent ?? "")
    .replace(/\s+/g, " ")
    .trim();
  if (kept && kept.length <= 80) return kept;
  // The first company-classed element that says something: the first one was
  // often the logo image, which says nothing.
  for (const el of Array.from(doc.querySelectorAll('[class*="company" i]'))) {
    const named = visibleText(el);
    if (named) return named.slice(0, 120);
  }
  return "";
}

export function extractJobContext(doc: Document = document): JobContext {
  try {
    return {
      jobDescription: extractDescription(doc),
      jobTitle: extractTitle(doc),
      company: extractCompany(doc),
    };
  } catch {
    return { jobDescription: "", jobTitle: "", company: "" };
  }
}

/**
 * Cheap company + title only, skips the expensive description scan. The panel's
 * job-card header re-derives this on every overlay refresh, so it must stay
 * lightweight (a few querySelector reads, no whole-page text walk).
 */
export function extractJobIdentity(doc: Document = document): { company: string; jobTitle: string } {
  try {
    return { company: extractCompany(doc), jobTitle: extractTitle(doc) };
  } catch {
    return { company: "", jobTitle: "" };
  }
}
