import {
  experienceLevelsOrDefault,
  normalizeCareerFamilies,
  type JobFilters,
} from "../lib/jobFilterOptions";
import type { SetupAnswers } from "./types";

/**
 * Pure mapping from wizard answers to the dashboard's JobFilters shape.
 * Only fields with a real JobFilters counterpart are mapped; job_types,
 * work_authorization, and target_titles are captured in settings elsewhere.
 * The returned object must match JobFilters exactly (written to
 * localStorage["job-aggregator-filters"], read on the Jobs page).
 * The job functions are career family slugs; no experience answer gives the
 * default levels, as on the Jobs page.
 */
export function answersToFilters(a: SetupAnswers): JobFilters {
  return {
    country: a.country,
    location: a.city.trim() ? [a.city.trim()] : [],
    work_type: a.open_to_remote ? ["remote"] : [],
    career_family: normalizeCareerFamilies(a.job_functions),
    experience_level: experienceLevelsOrDefault(a.experience_level ? [a.experience_level] : []),
    date_posted: "",
  };
}
