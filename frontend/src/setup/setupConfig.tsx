import type { SetupStep } from "./types";
import { WelcomeNameStep } from "./steps/WelcomeNameStep";
import { RolePreferencesStep } from "./steps/RolePreferencesStep";
import { ExperienceStep } from "./steps/ExperienceStep";

// ResumeStep is rendered specially by the wizard (needs file props), so it is
// NOT in this array: the wizard appends it as the final step.
export const SETUP_STEPS: SetupStep[] = [
  {
    id: "welcome",
    headline: "Welcome to <b>Tailrd</b>: let's set up your job search.",
    Component: WelcomeNameStep,
    validate: (a) => (a.first_name.trim() && a.last_name.trim() ? null : "Please enter your first and last name."),
  },
  {
    id: "role",
    headline: "To get started, <b>what type of role</b> are you looking for?",
    Component: RolePreferencesStep,
    validate: (a) => {
      if (a.job_functions.length === 0) return "Please select at least one job function.";
      if (!a.country) return "Please select a location.";
      return null;
    },
  },
  {
    id: "experience",
    headline: "What <b>level</b> of role are you looking for?",
    Component: ExperienceStep,
    validate: (a) => (a.experience_level ? null : "Please select what you're looking for."),
  },
];
