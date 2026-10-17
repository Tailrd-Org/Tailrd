/**
 * A site's own job search: never an application field. Mirrors consent.ts and
 * pageChrome.ts: the scanner skips these controls entirely.
 *
 * A closed iCIMS posting (DocuSign, live 2026-10-17) redirected to the
 * employer's job search, where Autofill typed the applicant's current title
 * into "Search Keyword, Category or Job Title" (and so ran a search) and sent
 * the filters and "Sort by" to the AI. A search box is not in a search
 * landmark there, so pageChrome.ts never saw it.
 *
 * Narrow on purpose: Workday's multiselect box (placeholder "Search", named by
 * its question) and a location typeahead ("Search for a city") are fields. A
 * control is a site search only by a search id, or when what it is called
 * starts with "Search" and names what a job search looks for.
 */

const SEARCH_ID =
  /^(keywords?|search-?keywords?|keywords?-?search|location-?search|search-?locations?|job-?search|search-?jobs?|search-?term|search-?text)$/i;
const SEARCH_NAME =
  /^search\b.{0,40}?\b(keywords?|jobs?|titles?|categor(y|ies)|roles?|positions?|openings?|careers?|locations?)\b/i;
/** The result list's own controls. */
const RESULT_CONTROL = /^(sort|sort by|sort results( by)?|order by|(items|results|jobs) per page)$/i;

function textOf(doc: Document, ids: string): string {
  return ids
    .split(/\s+/)
    .map((id) => doc.getElementById(id)?.textContent ?? "")
    .join(" ");
}

/** What the control is called: its aria-label, the elements naming it, its
 *  <label>; the placeholder only when nothing else names it. */
function nameOf(el: HTMLElement): string {
  const doc = el.ownerDocument;
  const clean = (t: string | null | undefined): string => (t ?? "").replace(/\s+/g, " ").trim();
  const labelledBy = el.getAttribute("aria-labelledby");
  const labels = (el as HTMLInputElement).labels;
  return (
    clean(el.getAttribute("aria-label")) ||
    clean(labelledBy ? textOf(doc, labelledBy) : "") ||
    clean(labels && labels.length > 0 ? labels[0].textContent : "") ||
    clean(el.getAttribute("placeholder"))
  );
}

export function isSiteSearch(el: HTMLElement): boolean {
  if (SEARCH_ID.test(el.id) || SEARCH_ID.test(el.getAttribute("name") ?? "")) return true;
  const name = nameOf(el);
  return SEARCH_NAME.test(name) || RESULT_CONTROL.test(name);
}
