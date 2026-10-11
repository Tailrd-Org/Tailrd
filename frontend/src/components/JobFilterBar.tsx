import { useState, useRef, useEffect, useCallback } from "react";
import { CaretDown, CaretUp } from "@phosphor-icons/react";
import api from "../auth/api";

export interface JobFilters {
  country: string;           // "US" | "CA" | ""
  location: string[];        // array of city search tags
  work_type: string[];       // ["remote", "hybrid", "onsite"]
  role_category: string[];   // selected categories
  experience_level: string[];  // ["internship", "new_grad"]
  date_posted: string;       // "24h" | "3d" | "week" | "month" | ""
}

interface JobFilterBarProps {
  filters: JobFilters;
  onChange: (filters: JobFilters) => void;
}

// --- Constants ---

export const COUNTRY_OPTIONS = [
  { value: "US", label: "United States" },
  { value: "CA", label: "Canada" },
];

export const JOB_FUNCTION_OPTIONS = [
  "Software Engineering",
  "Engineering and Development",
  "Data Analysis",
  "Machine Learning and AI",
  "Accounting and Finance",
  "Management and Executive",
  "Sales",
  "Marketing",
  "Human Resources",
  "Product Management",
  "Business Analyst",
  "Creatives and Design",
  "Legal and Compliance",
  "Customer Service and Support",
  "Operations",
  "Consultant",
  "Other",
];

// Values are the spellings stored in scraped_jobs.experience_level, the only two
// levels the catalogue actually contains. Labels carry both the US ("internship",
// "entry level") and Canadian ("co-op", "new grad") vocabulary.
export const EXPERIENCE_OPTIONS = [
  { value: "internship", label: "Internship / Co-op" },
  { value: "new_grad", label: "New Grad / Entry Level" },
];

const EXPERIENCE_VALUES = EXPERIENCE_OPTIONS.map((o) => o.value);

// Experience values persisted by the pre-2026-07 taxonomy, which offered levels the
// catalogue never had. Mid/senior/lead/director are absent on purpose: they matched
// zero jobs, so they are dropped rather than remapped.
const LEGACY_EXPERIENCE_MAP: Record<string, string[]> = {
  intern_new_grad: ["internship", "new_grad"],
  entry: ["new_grad"],
};

/**
 * Migrate persisted experience filters onto the current taxonomy.
 *
 * Saved filters outlive the options that produced them. Without this, a user holding
 * `["senior"]` in localStorage would carry an invisible filter, no checkbox renders
 * for it, so it cannot be cleared, and it silently returns an empty job list.
 * Unknown values are dropped, which restores an unfiltered view instead.
 */
export function normalizeExperienceLevels(values: unknown): string[] {
  const raw = Array.isArray(values) ? values : typeof values === "string" && values ? [values] : [];
  const out: string[] = [];
  for (const item of raw) {
    if (typeof item !== "string") continue;
    const key = item.trim().toLowerCase();
    if (!key) continue;
    const mapped = EXPERIENCE_VALUES.includes(key) ? [key] : (LEGACY_EXPERIENCE_MAP[key] ?? []);
    for (const value of mapped) {
      if (!out.includes(value)) out.push(value);
    }
  }
  return out;
}

const WORK_MODEL_OPTIONS = [
  { value: "onsite", label: "Onsite" },
  { value: "hybrid", label: "Hybrid" },
  { value: "remote", label: "Remote" },
];

const DATE_POSTED_OPTIONS = [
  { value: "24h", label: "Past 24 hours" },
  { value: "3d", label: "Past 3 days" },
  { value: "week", label: "Past week" },
  { value: "month", label: "Past month" },
  { value: "", label: "Any time" },
];

// --- Styles ---

const styles = {
  container: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    padding: "10px 0",
    flexWrap: "wrap" as const,
    position: "relative" as const,
  },
  pillButton: {
    display: "inline-flex",
    alignItems: "center",
    gap: "6px",
    padding: "8px 16px",
    borderRadius: "9999px",
    border: "1px solid #e3e8ee",
    background: "#fff",
    color: "#273951",
    fontSize: "14px",
    fontWeight: 400,
    cursor: "pointer",
    transition: "all 0.15s ease",
    whiteSpace: "nowrap" as const,
    userSelect: "none" as const,
  },
  pillButtonActive: {
    background: "#eef0ff",
    borderColor: "#c3b9fd",
    color: "#4434d4",
  },
  allFiltersButton: {
    background: "#533afd",
    borderColor: "#533afd",
    color: "#fff",
  },
  badge: {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    background: "#533afd",
    color: "#fff",
    fontSize: "11px",
    fontWeight: 500,
    borderRadius: "9999px",
    padding: "1px 6px",
    marginLeft: "2px",
    minWidth: "18px",
    height: "18px",
  },
  dropdown: {
    position: "absolute" as const,
    top: "100%",
    left: 0,
    marginTop: "8px",
    background: "#fff",
    borderRadius: "12px",
    border: "1px solid #e3e8ee",
    boxShadow: "0 8px 24px rgba(0,55,112,0.08), 0 2px 6px rgba(0,55,112,0.04)",
    padding: "16px",
    zIndex: 1000,
    minWidth: "260px",
    maxHeight: "400px",
    overflowY: "auto" as const,
  },
  dropdownTitle: {
    fontSize: "15px",
    fontWeight: 500,
    color: "#0d253d",
    marginBottom: "12px",
    letterSpacing: "-0.2px",
  },
  checkboxRow: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "7px 8px",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "14px",
    color: "#273951",
    transition: "background 0.1s",
  },
  checkboxRowHover: {
    background: "#eef0ff",
  },
  checkbox: {
    width: "16px",
    height: "16px",
    accentColor: "#533afd",
    cursor: "pointer",
  },
  radioInput: {
    width: "16px",
    height: "16px",
    accentColor: "#533afd",
    cursor: "pointer",
  },
  footerRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginTop: "12px",
    paddingTop: "12px",
    borderTop: "1px solid #e3e8ee",
  },
  resetBtn: {
    background: "none",
    border: "none",
    color: "#64748d",
    fontSize: "13px",
    cursor: "pointer",
    padding: "6px 12px",
    borderRadius: "9999px",
  },
  confirmBtn: {
    background: "#533afd",
    border: "none",
    color: "#fff",
    fontSize: "14px",
    fontWeight: 400,
    cursor: "pointer",
    padding: "8px 18px",
    borderRadius: "9999px",
  },
  searchInput: {
    width: "100%",
    padding: "8px 12px",
    borderRadius: "6px",
    border: "1px solid #a8c3de",
    fontSize: "14px",
    outline: "none",
    marginTop: "8px",
  },
  sectionLabel: {
    fontSize: "10px",
    fontWeight: 500,
    color: "#64748d",
    textTransform: "uppercase" as const,
    letterSpacing: "0.1px",
    marginTop: "12px",
    marginBottom: "6px",
  },
  chevron: {
    fontSize: "10px",
    marginLeft: "2px",
    opacity: 0.7,
  },
};

// --- Component ---

export default function JobFilterBar({ filters, onChange }: JobFilterBarProps) {
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Temp state for pending selections (before Confirm)
  const [tempCountry, setTempCountry] = useState(filters.country);
  const [tempLocationTags, setTempLocationTags] = useState<string[]>(filters.location);
  const [locationInput, setLocationInput] = useState("");
  const [tempRoleCategory, setTempRoleCategory] = useState<string[]>(filters.role_category);
  const [tempExperience, setTempExperience] = useState<string[]>(Array.isArray(filters.experience_level) ? filters.experience_level : filters.experience_level ? [filters.experience_level] : []);
  const [tempWorkType, setTempWorkType] = useState<string[]>(filters.work_type);
  const [tempDatePosted, setTempDatePosted] = useState(filters.date_posted);

  // Sync temp state when filters change externally
  useEffect(() => {
    setTempCountry(filters.country);
    setTempLocationTags(filters.location);
    setTempRoleCategory(filters.role_category);
    setTempExperience(Array.isArray(filters.experience_level) ? filters.experience_level : filters.experience_level ? [filters.experience_level] : []);
    setTempWorkType(filters.work_type);
    setTempDatePosted(filters.date_posted);
  }, [filters]);

  // --- City suggestions (canonical parsed cities from the catalogue) ---

  const [citySuggestions, setCitySuggestions] = useState<{ city: string; count: number }[]>([]);
  useEffect(() => {
    const q = locationInput.trim();
    if (q.length < 2) {
      setCitySuggestions([]);
      return;
    }
    const handle = setTimeout(async () => {
      try {
        const { data } = await api.get("/jobs/cities", {
          params: { q, country: tempCountry || undefined },
        });
        setCitySuggestions(Array.isArray(data) ? data : []);
      } catch {
        setCitySuggestions([]);
      }
    }, 200);
    return () => clearTimeout(handle);
  }, [locationInput, tempCountry]);

  // --- City tag management ---

  function addCityTag(city: string) {
    const trimmed = city.trim();
    if (!trimmed) return;
    setLocationInput("");
    setCitySuggestions([]);
    if (tempLocationTags.includes(trimmed)) return;
    setTempLocationTags((prev) => [...prev, trimmed]);
  }

  /**
   * Text sitting in the city input is a filter the user typed but never
   * committed with Enter, Confirm must not silently drop it. Returns the
   * effective tag list so confirm handlers don't race setState.
   */
  function commitPendingCityInput(): string[] {
    const trimmed = locationInput.trim();
    if (!trimmed) return tempLocationTags;
    setLocationInput("");
    setCitySuggestions([]);
    if (tempLocationTags.includes(trimmed)) return tempLocationTags;
    const next = [...tempLocationTags, trimmed];
    setTempLocationTags(next);
    return next;
  }

  function removeCityTag(city: string) {
    setTempLocationTags((prev) => prev.filter((tag) => tag !== city));
  }

  function handleLocationKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      addCityTag(locationInput);
    } else if (e.key === "Backspace" && locationInput === "" && tempLocationTags.length > 0) {
      setTempLocationTags((prev) => prev.slice(0, -1));
    }
  }

  // Close dropdown on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpenDropdown(null);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const toggleDropdown = useCallback((name: string) => {
    setOpenDropdown((prev) => (prev === name ? null : name));
  }, []);

  // --- Confirm handlers ---

  function confirmCountry() {
    onChange({ ...filters, country: tempCountry, location: commitPendingCityInput() });
    setOpenDropdown(null);
  }

  function confirmJobFunction() {
    onChange({ ...filters, role_category: tempRoleCategory });
    setOpenDropdown(null);
  }

  function confirmExperience() {
    onChange({ ...filters, experience_level: tempExperience });
    setOpenDropdown(null);
  }

  function confirmWorkModel() {
    onChange({ ...filters, work_type: tempWorkType });
    setOpenDropdown(null);
  }

  function confirmDatePosted() {
    onChange({ ...filters, date_posted: tempDatePosted });
    setOpenDropdown(null);
  }

  // --- Reset handlers ---

  function resetCountry() {
    setTempCountry("");
    setTempLocationTags([]);
    setLocationInput("");
  }

  function resetJobFunction() {
    setTempRoleCategory([]);
  }

  function resetExperience() {
    setTempExperience([]);
  }

  function resetWorkModel() {
    setTempWorkType([]);
  }

  function resetDatePosted() {
    setTempDatePosted("");
  }

  // --- Badge counts ---

  const countryActive = filters.country || filters.location.length > 0;
  const jobFunctionCount = filters.role_category.length;
  const experienceActive = filters.experience_level.length;
  const workModelCount = filters.work_type.length;
  const datePostedActive = filters.date_posted ? 1 : 0;

  return (
    <div ref={containerRef} style={styles.container} role="toolbar" aria-label="Job filters">
      {/* Country Filter */}
      <div style={{ position: "relative", display: "inline-block" }}>
        <FilterPill
          label="Country"
          isActive={!!countryActive}
          count={countryActive ? 1 : 0}
          isOpen={openDropdown === "country"}
          onClick={() => toggleDropdown("country")}
        />
        {openDropdown === "country" && (
          <DropdownPanel style={{}}>
            <div style={styles.dropdownTitle}>Country</div>
            {COUNTRY_OPTIONS.map((opt) => (
              <label key={opt.value} style={styles.checkboxRow}>
                <input
                  type="radio"
                  name="country"
                  style={styles.radioInput}
                  checked={tempCountry === opt.value}
                  onChange={() => setTempCountry(opt.value)}
                />
                {opt.label}
              </label>
            ))}
            <div style={styles.sectionLabel}>Location</div>
            <label style={styles.checkboxRow}>
              <input
                type="checkbox"
                style={styles.checkbox}
                checked={tempLocationTags.length === 0 && locationInput.trim() === ""}
                onChange={() => { setTempLocationTags([]); setLocationInput(""); setCitySuggestions([]); }}
              />
              All locations within {tempCountry === "CA" ? "Canada" : "United States"}
            </label>
            <div style={{
              display: "flex",
              flexWrap: "wrap",
              alignItems: "center",
              gap: "6px",
              padding: "8px 12px",
              borderRadius: "6px",
              border: "1px solid #a8c3de",
              marginTop: "8px",
              minHeight: "38px",
            }}>
              {tempLocationTags.map((tag) => (
                <span
                  key={tag}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "4px",
                    padding: "4px 10px",
                    borderRadius: "9999px",
                    background: "#eef0ff",
                    border: "1px solid #c3b9fd",
                    color: "#4434d4",
                    fontSize: "12px",
                    fontWeight: 400,
                    whiteSpace: "nowrap",
                  }}
                >
                  {tag}
                  <button
                    type="button"
                    tabIndex={0}
                    role="button"
                    onClick={() => removeCityTag(tag)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        removeCityTag(tag);
                      }
                    }}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      background: "none",
                      border: "none",
                      color: "#533afd",
                      fontSize: "14px",
                      fontWeight: 600,
                      cursor: "pointer",
                      padding: "0 2px",
                      lineHeight: 1,
                    }}
                    aria-label={`Remove ${tag} filter`}
                  >
                    ×
                  </button>
                </span>
              ))}
              <input
                type="text"
                placeholder={tempLocationTags.length === 0 ? "Enter City" : "Add another city"}
                value={locationInput}
                onChange={(e) => setLocationInput(e.target.value)}
                onKeyDown={handleLocationKeyDown}
                aria-label="Type a city name and press Enter to add"
                style={{
                  flex: 1,
                  minWidth: "80px",
                  border: "none",
                  outline: "none",
                  fontSize: "14px",
                  padding: "2px 0",
                }}
              />
            </div>
            {citySuggestions.length > 0 && (
              <div
                style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "8px" }}
                role="listbox"
                aria-label="City suggestions"
              >
                {citySuggestions.map((s) => (
                  <button
                    key={s.city}
                    type="button"
                    role="option"
                    aria-selected={false}
                    style={{ ...styles.pillButton, padding: "4px 10px", fontSize: "12px" }}
                    onClick={() => addCityTag(s.city)}
                  >
                    {s.city} <span style={{ color: "#64748d" }}>({s.count})</span>
                  </button>
                ))}
              </div>
            )}
            <DropdownFooter onReset={resetCountry} onConfirm={confirmCountry} />
          </DropdownPanel>
        )}
      </div>

      {/* Job Function Filter */}
      <div style={{ position: "relative", display: "inline-block" }}>
        <FilterPill
          label="Job Function"
          isActive={jobFunctionCount > 0}
          count={jobFunctionCount}
          isOpen={openDropdown === "jobFunction"}
          onClick={() => toggleDropdown("jobFunction")}
        />
        {openDropdown === "jobFunction" && (
          <DropdownPanel style={{ minWidth: "280px" }}>
            <div style={styles.dropdownTitle}>Job Function</div>
            <div style={{ maxHeight: "280px", overflowY: "auto" }}>
              {JOB_FUNCTION_OPTIONS.map((cat) => (
                <label key={cat} style={styles.checkboxRow}>
                  <input
                    type="checkbox"
                    style={styles.checkbox}
                    checked={tempRoleCategory.includes(cat)}
                    onChange={() => {
                      setTempRoleCategory((prev) =>
                        prev.includes(cat) ? prev.filter((c) => c !== cat) : [...prev, cat]
                      );
                    }}
                  />
                  {cat}
                </label>
              ))}
            </div>
            <DropdownFooter onReset={resetJobFunction} onConfirm={confirmJobFunction} />
          </DropdownPanel>
        )}
      </div>

      {/* Experience Level Filter */}
      <div style={{ position: "relative", display: "inline-block" }}>
        <FilterPill
          label="Experience Level"
          isActive={experienceActive > 0}
          count={experienceActive}
          isOpen={openDropdown === "experience"}
          onClick={() => toggleDropdown("experience")}
        />
        {openDropdown === "experience" && (
          <DropdownPanel style={{}}>
            <div style={styles.dropdownTitle}>Experience Level</div>
            {EXPERIENCE_OPTIONS.map((opt) => (
              <label key={opt.value} style={styles.checkboxRow}>
                <input
                  type="checkbox"
                  style={styles.checkbox}
                  checked={tempExperience.includes(opt.value)}
                  onChange={() => {
                    setTempExperience((prev) =>
                      prev.includes(opt.value) ? prev.filter((v) => v !== opt.value) : [...prev, opt.value]
                    );
                  }}
                />
                {opt.label}
              </label>
            ))}
            <DropdownFooter onReset={resetExperience} onConfirm={confirmExperience} />
          </DropdownPanel>
        )}
      </div>

      {/* Work Model Filter */}
      <div style={{ position: "relative", display: "inline-block" }}>
        <FilterPill
          label="Work Model"
          isActive={workModelCount > 0}
          count={workModelCount}
          isOpen={openDropdown === "workModel"}
          onClick={() => toggleDropdown("workModel")}
        />
        {openDropdown === "workModel" && (
          <DropdownPanel style={{}}>
            <div style={styles.dropdownTitle}>Work Model</div>
            {WORK_MODEL_OPTIONS.map((opt) => (
              <label key={opt.value} style={styles.checkboxRow}>
                <input
                  type="checkbox"
                  style={styles.checkbox}
                  checked={tempWorkType.includes(opt.value)}
                  onChange={() => {
                    setTempWorkType((prev) =>
                      prev.includes(opt.value) ? prev.filter((v) => v !== opt.value) : [...prev, opt.value]
                    );
                  }}
                />
                {opt.label}
              </label>
            ))}
            <DropdownFooter onReset={resetWorkModel} onConfirm={confirmWorkModel} />
          </DropdownPanel>
        )}
      </div>

      {/* Date Posted Filter */}
      <div style={{ position: "relative", display: "inline-block" }}>
        <FilterPill
          label="Date Posted"
          isActive={datePostedActive > 0}
          count={datePostedActive}
          isOpen={openDropdown === "datePosted"}
          onClick={() => toggleDropdown("datePosted")}
        />
        {openDropdown === "datePosted" && (
          <DropdownPanel style={{}}>
            <div style={styles.dropdownTitle}>Date Posted</div>
            {DATE_POSTED_OPTIONS.map((opt) => (
              <label key={opt.value} style={styles.checkboxRow}>
                <input
                  type="radio"
                  name="datePosted"
                  style={styles.radioInput}
                  checked={tempDatePosted === opt.value}
                  onChange={() => setTempDatePosted(opt.value)}
                />
                {opt.label}
              </label>
            ))}
            <DropdownFooter onReset={resetDatePosted} onConfirm={confirmDatePosted} />
          </DropdownPanel>
        )}
      </div>

      {/* All Filters Button */}
      <div style={{ position: "relative", display: "inline-block" }}>
        <button
          style={{ ...styles.pillButton, ...styles.allFiltersButton }}
          onClick={() => toggleDropdown("allFilters")}
          aria-label="All Filters"
        >
          ••• All Filters
        </button>
        {openDropdown === "allFilters" && (
          <DropdownPanel style={{ minWidth: "340px", maxHeight: "500px" }}>
            <div style={styles.dropdownTitle}>All Filters</div>

            <div style={styles.sectionLabel}>Country</div>
            {COUNTRY_OPTIONS.map((opt) => (
              <label key={opt.value} style={styles.checkboxRow}>
                <input
                  type="radio"
                  name="allCountry"
                  style={styles.radioInput}
                  checked={tempCountry === opt.value}
                  onChange={() => setTempCountry(opt.value)}
                />
                {opt.label}
              </label>
            ))}

            <div style={styles.sectionLabel}>Work Model</div>
            {WORK_MODEL_OPTIONS.map((opt) => (
              <label key={opt.value} style={styles.checkboxRow}>
                <input
                  type="checkbox"
                  style={styles.checkbox}
                  checked={tempWorkType.includes(opt.value)}
                  onChange={() => {
                    setTempWorkType((prev) =>
                      prev.includes(opt.value) ? prev.filter((v) => v !== opt.value) : [...prev, opt.value]
                    );
                  }}
                />
                {opt.label}
              </label>
            ))}

            <div style={styles.sectionLabel}>Experience Level</div>
            {EXPERIENCE_OPTIONS.map((opt) => (
              <label key={opt.value} style={styles.checkboxRow}>
                <input
                  type="checkbox"
                  style={styles.checkbox}
                  checked={tempExperience.includes(opt.value)}
                  onChange={() => {
                    setTempExperience((prev) =>
                      prev.includes(opt.value) ? prev.filter((v) => v !== opt.value) : [...prev, opt.value]
                    );
                  }}
                />
                {opt.label}
              </label>
            ))}

            <div style={styles.sectionLabel}>Date Posted</div>
            {DATE_POSTED_OPTIONS.map((opt) => (
              <label key={opt.value} style={styles.checkboxRow}>
                <input
                  type="radio"
                  name="allDatePosted"
                  style={styles.radioInput}
                  checked={tempDatePosted === opt.value}
                  onChange={() => setTempDatePosted(opt.value)}
                />
                {opt.label}
              </label>
            ))}

            <DropdownFooter
              onReset={() => {
                resetCountry();
                resetJobFunction();
                resetExperience();
                resetWorkModel();
                resetDatePosted();
              }}
              onConfirm={() => {
                onChange({
                  ...filters,
                  country: tempCountry,
                  location: commitPendingCityInput(),
                  work_type: tempWorkType,
                  role_category: tempRoleCategory,
                  experience_level: tempExperience,
                  date_posted: tempDatePosted,
                });
                setOpenDropdown(null);
              }}
            />
          </DropdownPanel>
        )}
      </div>
    </div>
  );
}

// --- Sub-components ---

function FilterPill({
  label,
  isActive,
  count,
  isOpen,
  onClick,
}: {
  label: string;
  isActive: boolean;
  count: number;
  isOpen: boolean;
  onClick: () => void;
}) {
  const pillStyle = {
    ...styles.pillButton,
    ...(isActive ? styles.pillButtonActive : {}),
    ...(isOpen ? { borderColor: "#533afd", boxShadow: "0 0 0 3px rgba(83,58,253,0.12)" } : {}),
  };

  return (
    <button style={pillStyle} onClick={onClick} aria-expanded={isOpen} aria-haspopup="true">
      {label}
      {count > 0 && <span style={styles.badge}>+{count}</span>}
      <span style={{ display: "inline-flex", marginLeft: "2px", opacity: 0.7 }}>
        {isOpen ? <CaretUp size={12} weight="bold" /> : <CaretDown size={12} weight="bold" />}
      </span>
    </button>
  );
}

function DropdownPanel({
  children,
  style: extraStyle,
}: {
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div style={{ ...styles.dropdown, ...extraStyle }} role="dialog" aria-modal="false">
      {children}
    </div>
  );
}

function DropdownFooter({ onReset, onConfirm }: { onReset: () => void; onConfirm: () => void }) {
  return (
    <div style={styles.footerRow}>
      <button style={styles.resetBtn} onClick={onReset}>
        Reset
      </button>
      <button style={styles.confirmBtn} onClick={onConfirm}>
        Confirm
      </button>
    </div>
  );
}
