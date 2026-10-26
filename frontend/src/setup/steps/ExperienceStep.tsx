import { useEffect } from "react";
import type { StepProps } from "../types";
import { availableExperienceOptions } from "../../lib/jobFilterOptions";

// "Internship" is deliberately absent, the experience question above owns that
// concept, and offering it in both places is what made the page ambiguous.
const JOB_TYPES = [
  { value: "full_time", label: "Full-time" },
  { value: "part_time", label: "Part-time" },
  { value: "contract", label: "Contract" },
];

function toggle(list: string[], v: string): string[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

export function ExperienceStep({ answers, update, experienceLevels }: StepProps) {
  // Only the levels that have live jobs behind them: an option with none is a dead end.
  const options = availableExperienceOptions(experienceLevels);
  const pickIsOffered = !answers.experience_level || options.some((o) => o.value === answers.experience_level);
  useEffect(() => {
    // The list can arrive after a pick was made. A pick that turns out to have no jobs
    // behind it is cleared, so the student chooses again from what is there.
    if (!pickIsOffered) update({ experience_level: "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pickIsOffered]);

  return (
    <>
      <div className="setup-field">
        <label className="setup-label"><span className="req">*</span>I'm looking for</label>
        <div className="setup-checkgrid">
          {options.map((opt) => (
            <label key={opt.value} className={`setup-check${answers.experience_level === opt.value ? " checked" : ""}`}>
              <input type="radio" name="experience" checked={answers.experience_level === opt.value}
                onChange={() => update({ experience_level: opt.value })} />
              {opt.label}
            </label>
          ))}
        </div>
      </div>

      <div className="setup-field">
        <label className="setup-label" htmlFor="setup-expected-graduation">Expected graduation (optional)</label>
        <input id="setup-expected-graduation" type="month" className="setup-input" value={answers.expected_graduation}
          onChange={(e) => update({ expected_graduation: e.target.value })} />
      </div>

      <div className="setup-field">
        <label className="setup-label">Job Type</label>
        <div className="setup-checkgrid">
          {JOB_TYPES.map((t) => (
            <label key={t.value} className={`setup-check${answers.job_types.includes(t.value) ? " checked" : ""}`}>
              <input type="checkbox" checked={answers.job_types.includes(t.value)}
                onChange={() => update({ job_types: toggle(answers.job_types, t.value) })} />
              {t.label}
            </label>
          ))}
        </div>
      </div>
    </>
  );
}
