// chrome-extension/src/content/jobContextRefresh.ts
/**
 * The job's country, city and company, read once per URL: the question
 * resolver answers "authorized to work in THIS country?" for the job's
 * country, and "located here, or would you relocate?" for its city. Then read
 * again on the same page until the page names them.
 */
import { getResolveContext, setResolveContext } from "./fieldResolver";
import { extractJobIdentity } from "./jobContext";
import { detectJobPlace, sanitizeCompany, type JobPlace } from "./jobLocation";

export interface JobContextReader {
  /** The page last read. */
  readonly url: string;
  /** Read the page at `href`. True when it is a page not read before whose
   *  own place names no country: the caller looks outside the page for one. */
  refresh(doc: Document, href: string): boolean;
}

export function createJobContextReader(): JobContextReader {
  let url = "";
  let placeFound = false;
  return {
    get url() {
      return url;
    },
    refresh(doc: Document, href: string): boolean {
      if (url === href) {
        // Greenhouse's job-boards page names its company only in its title,
        // and one run in three read the page before the title was there
        // (Anduril, live, round 8: "Company website" took "Other" over
        // "Anduril Website"). A company already known (one adopted from the
        // page around a frame) stays.
        if (!getResolveContext().company) {
          try {
            const company = sanitizeCompany(extractJobIdentity(doc).company);
            if (company) setResolveContext({ company });
          } catch {
            // Still unnamed; the next read tries again.
          }
        }
        // Workable renders the posting's header after the first scan (Mindex,
        // live 2026-10-03: "onsite at our Rochester office?" never saw
        // Rochester, New York). Read again until the page names a place; a place
        // adopted from outside stays unless the page itself names one.
        if (placeFound) return false;
        try {
          const late = detectJobPlace(doc);
          if (late.country || late.places?.length) {
            placeFound = true;
            setResolveContext({ jobCountry: late.country ?? getResolveContext().jobCountry, jobCity: late.city, jobPlaces: late.places ?? null });
          }
        } catch {
          // The page's own place stays unknown; nothing to change.
        }
        return false;
      }
      url = href;
      let place: JobPlace = { country: null, city: null };
      try {
        place = detectJobPlace(doc);
        setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: sanitizeCompany(extractJobIdentity(doc).company) });
      } catch {
        setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
      }
      placeFound = Boolean(place.country || place.places?.length);
      return !place.country;
    },
  };
}
