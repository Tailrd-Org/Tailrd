import type { ComponentType } from "react";

export interface SetupAnswers {
  first_name: string;
  last_name: string;
  job_functions: string[];      // CAREER_FAMILY_OPTIONS values (slugs) -> career_family
  job_types: string[];          // "full_time"|"part_time"|"contract" (captured only)
  country: string;              // "CA" | "US" | ""
  city: string;
  open_to_remote: boolean;
  work_authorization: string[]; // e.g. ["needs_sponsorship"] (captured only)
  experience_level: string;     // one EXPERIENCE_OPTIONS value: "intern_new_grad" | "entry" | ... | "director"
  // Screening answers the extension fills employer questions from, saved to the
  // application profile on finish (2026-10-03). "" = not answered.
  authorized_canada: "" | "yes" | "no";
  authorized_us: "" | "yes" | "no";
  expected_graduation: string;  // "YYYY-MM"
}

export interface StepProps {
  answers: SetupAnswers;
  update: (patch: Partial<SetupAnswers>) => void;
  // The Experience Level options that have live jobs (GET /jobs/levels); null until
  // known. Only the experience step reads it.
  experienceLevels?: readonly string[] | null;
}

export interface SetupStep {
  id: string;
  headline: string;                         // left assistant-panel headline
  Component: ComponentType<StepProps>;
  validate?: (a: SetupAnswers) => string | null; // error string or null
}

export const emptyAnswers: SetupAnswers = {
  first_name: "",
  last_name: "",
  job_functions: [],
  job_types: [],
  country: "",
  city: "",
  open_to_remote: false,
  work_authorization: [],
  experience_level: "",
  authorized_canada: "",
  authorized_us: "",
  expected_graduation: "",
};
