import { describe, it, expect } from "vitest";
import * as fc from "fast-check";
import {
  CAREER_FAMILY_OPTIONS,
  LEGACY_CATEGORY_TO_FAMILIES,
  careerFamilyLabel,
  normalizeCareerFamilies,
} from "../components/JobFilterBar";

const SLUGS = CAREER_FAMILY_OPTIONS.map((o) => o.value);

// The 17 job functions the filter bar and onboarding offered before the families.
const OLD_JOB_FUNCTIONS = [
  "Software Engineering", "Engineering and Development", "Data Analysis",
  "Machine Learning and AI", "Accounting and Finance", "Management and Executive", "Sales",
  "Marketing", "Human Resources", "Product Management", "Business Analyst",
  "Creatives and Design", "Legal and Compliance", "Customer Service and Support",
  "Operations", "Consultant", "Other",
];

describe("CAREER_FAMILY_OPTIONS", () => {
  it("are the 20 families, slugs in order, ending with Other", () => {
    expect(SLUGS).toEqual([
      "software_engineering", "data_ai_analytics", "it_security_infrastructure",
      "product_design", "engineering", "legal", "healthcare_clinical",
      "healthcare_admin_support", "finance_accounting", "banking_insurance_investment",
      "sales_bizdev", "marketing_communications", "operations_supply_chain", "hr_recruiting",
      "education_research", "government_public_sector", "skilled_trades",
      "customer_support_admin", "hospitality_retail_food", "other",
    ]);
  });

  it("carry no comma in a value, since the API's list filters are comma-separated", () => {
    expect(SLUGS.every((s) => !s.includes(","))).toBe(true);
    expect(careerFamilyLabel("healthcare_clinical")).toBe("Healthcare, Clinical");
  });
});

describe("LEGACY_CATEGORY_TO_FAMILIES", () => {
  it("maps every old job function onto real families", () => {
    expect(Object.keys(LEGACY_CATEGORY_TO_FAMILIES).sort()).toEqual([...OLD_JOB_FUNCTIONS].sort());
    for (const families of Object.values(LEGACY_CATEGORY_TO_FAMILIES)) {
      expect(families.length).toBeGreaterThan(0);
      expect(families.every((f) => SLUGS.includes(f))).toBe(true);
    }
  });
});

describe("normalizeCareerFamilies", () => {
  it("keeps known slugs, case- and space-tolerant, deduped", () => {
    expect(normalizeCareerFamilies(["legal", " Legal ", "SKILLED_TRADES"])).toEqual([
      "legal",
      "skilled_trades",
    ]);
  });

  it("migrates saved job functions through the legacy map", () => {
    expect(normalizeCareerFamilies([], ["Software Engineering"])).toEqual(["software_engineering"]);
    expect(normalizeCareerFamilies(undefined, ["Engineering and Development", "Data Analysis"])).toEqual([
      "engineering",
      "it_security_infrastructure",
      "data_ai_analytics",
    ]);
    expect(normalizeCareerFamilies(["data_ai_analytics"], ["Machine Learning and AI", "Consultant"])).toEqual([
      "data_ai_analytics",
      "other",
    ]);
  });

  it("drops anything that is neither a family nor an old job function", () => {
    expect(normalizeCareerFamilies(["Software Engineering", "astronaut"], ["Arts and Entertainment"])).toEqual([]);
  });

  it("survives malformed persisted state", () => {
    expect(normalizeCareerFamilies(undefined)).toEqual([]);
    expect(normalizeCareerFamilies(null, null)).toEqual([]);
    expect(normalizeCareerFamilies("legal", "Sales")).toEqual(["legal", "sales_bizdev"]);
    expect(normalizeCareerFamilies([42, {}, ""], [null, 7])).toEqual([]);
  });

  it("only ever yields family slugs, and is idempotent", () => {
    const anyValue = fc.oneof(
      fc.constantFrom(...SLUGS, ...OLD_JOB_FUNCTIONS, "Healthcare, Clinical"),
      fc.string(),
      fc.constant(null),
    );
    fc.assert(
      fc.property(fc.array(anyValue), fc.array(anyValue), (families, legacy) => {
        const once = normalizeCareerFamilies(families, legacy);
        expect(once.every((v) => SLUGS.includes(v))).toBe(true);
        expect(new Set(once).size).toBe(once.length);
        expect(normalizeCareerFamilies(once)).toEqual(once);
      }),
      { numRuns: 200 },
    );
  });
});

describe("careerFamilyLabel", () => {
  it("names a family and is blank for anything else", () => {
    expect(careerFamilyLabel("skilled_trades")).toBe("Skilled Trades, Construction & Manufacturing");
    expect(careerFamilyLabel("Software Engineering")).toBe("");
    expect(careerFamilyLabel(null)).toBe("");
  });
});
