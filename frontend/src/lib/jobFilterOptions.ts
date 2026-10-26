// The Jobs filters' option lists, their normalizers and the saved-filter migration.
// Pure data and functions, apart from the JobFilterBar component (which re-exports
// all of this), so pages and onboarding use them without loading the component.

export interface JobFilters {
  country: string;           // "US" | "CA" | ""
  location: string[];        // array of city search tags
  work_type: string[];       // ["remote", "hybrid", "onsite"]
  career_family: string[];   // CAREER_FAMILY_OPTIONS values (slugs)
  experience_level: string[];  // EXPERIENCE_OPTIONS values, never empty (see DEFAULT_EXPERIENCE_LEVELS)
  date_posted: string;       // "24h" | "3d" | "week" | "month" | ""
}

// The job functions are the 20 career families of scraped_jobs.career_family (the
// backend's ingest/taxonomy.py FAMILIES, in its order). Values are slugs because five
// labels contain commas and the API's list filters are comma-separated. A backend
// test (test_taxonomy_frontend_agreement.py) checks this list and the legacy map
// below against taxonomy.py.
export const CAREER_FAMILY_OPTIONS = [
  { value: "software_engineering", label: "Software Engineering" },
  { value: "data_ai_analytics", label: "Data, AI & Analytics" },
  { value: "it_security_infrastructure", label: "IT, Security & Infrastructure" },
  { value: "product_design", label: "Product & Design" },
  { value: "engineering", label: "Engineering" },
  { value: "legal", label: "Legal" },
  { value: "healthcare_clinical", label: "Healthcare, Clinical" },
  { value: "healthcare_admin_support", label: "Healthcare, Administration & Support" },
  { value: "finance_accounting", label: "Finance & Accounting" },
  { value: "banking_insurance_investment", label: "Banking, Insurance & Investment" },
  { value: "sales_bizdev", label: "Sales & Business Development" },
  { value: "marketing_communications", label: "Marketing & Communications" },
  { value: "operations_supply_chain", label: "Operations, Supply Chain & Logistics" },
  { value: "hr_recruiting", label: "Human Resources & Recruiting" },
  { value: "education_research", label: "Education & Research" },
  { value: "government_public_sector", label: "Government & Public Sector" },
  { value: "skilled_trades", label: "Skilled Trades, Construction & Manufacturing" },
  { value: "customer_support_admin", label: "Customer Support & Administrative" },
  { value: "hospitality_retail_food", label: "Hospitality, Retail & Food Service" },
  { value: "other", label: "Other" },
];

const FAMILY_VALUES = CAREER_FAMILY_OPTIONS.map((o) => o.value);

// The 17 job functions saved filters and onboarding used before the families (the
// role_category values) -> the families they become. Identical to the backend's
// taxonomy.LEGACY_CATEGORY_TO_FAMILIES.
export const LEGACY_CATEGORY_TO_FAMILIES: Record<string, string[]> = {
  "Software Engineering": ["software_engineering"],
  "Engineering and Development": ["engineering", "it_security_infrastructure"],
  "Data Analysis": ["data_ai_analytics"],
  "Machine Learning and AI": ["data_ai_analytics"],
  "Accounting and Finance": ["finance_accounting", "banking_insurance_investment"],
  "Management and Executive": ["operations_supply_chain"],
  "Sales": ["sales_bizdev"],
  "Marketing": ["marketing_communications"],
  "Human Resources": ["hr_recruiting"],
  "Product Management": ["product_design"],
  "Business Analyst": ["data_ai_analytics"],
  "Creatives and Design": ["product_design", "marketing_communications"],
  "Legal and Compliance": ["legal"],
  "Customer Service and Support": ["customer_support_admin"],
  "Operations": ["operations_supply_chain"],
  "Consultant": ["other"],
  "Other": ["other"],
};

/** A family slug as people read it ("" for anything that is not a family). */
export function careerFamilyLabel(slug: string | null | undefined): string {
  return CAREER_FAMILY_OPTIONS.find((o) => o.value === slug)?.label ?? "";
}

function stringList(values: unknown): string[] {
  const raw = Array.isArray(values) ? values : typeof values === "string" && values ? [values] : [];
  return raw.filter((item): item is string => typeof item === "string");
}

/**
 * Migrate a persisted job-function filter onto the career families.
 *
 * `families` are slugs saved by the current page; `legacyCategories` are the old
 * job-function names (`role_category`) a filter or onboarding saved before, mapped
 * through LEGACY_CATEGORY_TO_FAMILIES. Anything else is dropped, so no value without
 * a checkbox survives as an invisible filter that cannot be cleared.
 */
export function normalizeCareerFamilies(families: unknown, legacyCategories?: unknown): string[] {
  const out: string[] = [];
  const add = (value: string) => {
    if (!out.includes(value)) out.push(value);
  };
  for (const item of stringList(families)) {
    const key = item.trim().toLowerCase();
    if (FAMILY_VALUES.includes(key)) add(key);
  }
  for (const item of stringList(legacyCategories)) {
    for (const family of LEGACY_CATEGORY_TO_FAMILIES[item.trim()] ?? []) add(family);
  }
  return out;
}

// Jobright's six levels. Two options cover two stored levels each (the backend's
// EXPERIENCE_FILTER_MAP): Intern/New Grad is internship + new_grad, and
// Director/Executive is director + executive.
export const EXPERIENCE_OPTIONS = [
  { value: "intern_new_grad", label: "Intern/New Grad" },
  { value: "entry", label: "Entry Level" },
  { value: "mid", label: "Mid Level" },
  { value: "senior", label: "Senior Level" },
  { value: "lead", label: "Lead/Staff" },
  { value: "director", label: "Director/Executive" },
];

// What someone who never chose levels sees: the student catalogue Tailrd has always
// shown. They can widen it from the Experience Level filter.
export const DEFAULT_EXPERIENCE_LEVELS = ["intern_new_grad", "entry"];

const EXPERIENCE_VALUES = EXPERIENCE_OPTIONS.map((o) => o.value);

// Values the two-option UI saved: "Internship / Co-op" and "New Grad / Entry Level".
// The six options cannot say "new grad without internships", so new_grad widens to
// Intern/New Grad and keeps the entry level it always included.
const LEGACY_EXPERIENCE_MAP: Record<string, string[]> = {
  internship: ["intern_new_grad"],
  new_grad: ["intern_new_grad", "entry"],
};

/**
 * Migrate persisted experience filters onto the current options.
 *
 * Saved filters outlive the options that produced them. Without this, a user holding
 * an old value in localStorage would carry an invisible filter: no checkbox renders
 * for it, so it cannot be cleared, and it silently returns an empty job list.
 * Unknown values are dropped.
 */
export function normalizeExperienceLevels(values: unknown): string[] {
  const out: string[] = [];
  for (const item of stringList(values)) {
    const key = item.trim().toLowerCase();
    if (!key) continue;
    const mapped = EXPERIENCE_VALUES.includes(key) ? [key] : (LEGACY_EXPERIENCE_MAP[key] ?? []);
    for (const value of mapped) {
      if (!out.includes(value)) out.push(value);
    }
  }
  return out;
}

/**
 * The levels a filter applies: the saved ones, or DEFAULT_EXPERIENCE_LEVELS when none
 * survive. The level filter is never empty: an empty one used to mean "no filter",
 * which once every level is in the catalogue would show a student Director roles.
 */
export function experienceLevelsOrDefault(values: unknown): string[] {
  const levels = normalizeExperienceLevels(values);
  return levels.length > 0 ? levels : [...DEFAULT_EXPERIENCE_LEVELS];
}

/**
 * GET /jobs/levels: the option values that have live jobs behind them. Anything the
 * pages cannot use (an error page from a proxy, an older backend, an empty list, only
 * keys that are not options) is null, which means "not known": see
 * availableExperienceOptions.
 */
export function parseAvailableLevels(data: unknown): string[] | null {
  const list = (data as { levels?: unknown } | null | undefined)?.levels;
  if (!Array.isArray(list)) return null;
  const keys = list.filter((v): v is string => typeof v === "string" && EXPERIENCE_VALUES.includes(v));
  return keys.length > 0 ? keys : null;
}

/**
 * The options worth offering, given the ones that have live jobs (parseAvailableLevels).
 *
 * An option with no jobs behind it is a silent dead end: ticking it returns an empty
 * feed. Until every level is ingested, production stores only the entry-level tiers
 * (internships and new grads, and Entry Level for junior and associate titles), so
 * the filter bar and onboarding offer just what the catalogue holds. The
 * options keep EXPERIENCE_OPTIONS' order and labels. When the list is not known
 * (still loading, the request failed) every option stays on offer, so a failed lookup
 * never takes the filter away.
 */
export function availableExperienceOptions(
  available: readonly string[] | null | undefined,
): typeof EXPERIENCE_OPTIONS {
  if (!available) return EXPERIENCE_OPTIONS;
  const offered = EXPERIENCE_OPTIONS.filter((o) => available.includes(o.value));
  return offered.length > 0 ? offered : EXPERIENCE_OPTIONS;
}

/**
 * experienceLevelsOrDefault, held to the options on offer.
 *
 * A saved level whose option is hidden has no checkbox, so it could not be cleared and
 * it would return an empty feed: it is dropped from the active filter. When nothing is
 * left, the default levels that are on offer, and when none of those are, every option
 * on offer. The result is never empty.
 */
export function experienceLevelsOnOffer(
  values: unknown,
  available: readonly string[] | null | undefined,
): string[] {
  const offered = availableExperienceOptions(available).map((o) => o.value);
  const kept = normalizeExperienceLevels(values).filter((v) => offered.includes(v));
  if (kept.length > 0) return kept;
  const defaults = DEFAULT_EXPERIENCE_LEVELS.filter((v) => offered.includes(v));
  return defaults.length > 0 ? defaults : offered;
}

// scraped_jobs.experience_level -> what a job card says.
const LEVEL_LABELS: Record<string, string> = {
  internship: "Internship",
  new_grad: "New Grad",
  entry: "Entry Level",
  mid: "Mid Level",
  senior: "Senior Level",
  lead: "Lead/Staff",
  director: "Director",
  executive: "Executive",
};

/** A stored experience level as people read it ("" when it is not one). */
export function experienceLevelLabel(level: string | null | undefined): string {
  return LEVEL_LABELS[(level ?? "").trim().toLowerCase()] ?? "";
}

/** The Jobs page filters for someone with nothing saved. */
export function defaultFilters(): JobFilters {
  return {
    country: "",
    location: [],
    work_type: [],
    career_family: [],
    experience_level: [...DEFAULT_EXPERIENCE_LEVELS],
    date_posted: "",
  };
}

/**
 * The Jobs page filters from what localStorage["job-aggregator-filters"] holds (written
 * by the page itself or by onboarding), migrating every older shape: a single location
 * string, the old job functions (`role_category`) and the old experience values. A
 * missing, empty or unreadable entry gives defaultFilters().
 */
export function restoreFilters(saved: string | null): JobFilters {
  if (!saved) return defaultFilters();
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(saved);
  } catch {
    return defaultFilters();
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return defaultFilters();
  return {
    country: typeof parsed.country === "string" ? parsed.country : "",
    location: Array.isArray(parsed.location)
      ? parsed.location
      : typeof parsed.location === "string" && parsed.location
        ? [parsed.location]
        : [],
    work_type: Array.isArray(parsed.work_type) ? parsed.work_type : [],
    career_family: normalizeCareerFamilies(parsed.career_family, parsed.role_category),
    experience_level: experienceLevelsOrDefault(parsed.experience_level),
    date_posted: typeof parsed.date_posted === "string" ? parsed.date_posted : "",
  };
}
