import type { StepProps } from "../types";
import { CAREER_FAMILY_OPTIONS, COUNTRY_OPTIONS } from "../../components/JobFilterBar";

function toggle(list: string[], v: string): string[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

// Asked here because almost every application asks them, per country, and no
// profile text can answer them reliably (the extension fills them from these).
const AUTH_QUESTIONS = [
  ["authorized_canada", "Are you authorized to work in Canada?"],
  ["authorized_us", "Are you authorized to work in the US?"],
] as const;

export function RolePreferencesStep({ answers, update }: StepProps) {
  return (
    <>
      <div className="setup-field">
        <label className="setup-label"><span className="req">*</span>Job Function</label>
        <div className="setup-checkgrid">
          {/* The 20 career families; answers keep the slug, the box shows the label. */}
          {CAREER_FAMILY_OPTIONS.map((fn) => (
            <label key={fn.value} className={`setup-check${answers.job_functions.includes(fn.value) ? " checked" : ""}`}>
              <input type="checkbox" checked={answers.job_functions.includes(fn.value)}
                onChange={() => update({ job_functions: toggle(answers.job_functions, fn.value) })} />
              {fn.label}
            </label>
          ))}
        </div>
      </div>

      <div className="setup-field">
        <label className="setup-label"><span className="req">*</span>Location</label>
        <div className="setup-checkgrid">
          <select className="setup-select" value={answers.country}
            onChange={(e) => update({ country: e.target.value })}>
            <option value="">Select country</option>
            {COUNTRY_OPTIONS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
          <input className="setup-input" value={answers.city}
            onChange={(e) => update({ city: e.target.value })} placeholder="City (optional)" />
        </div>
        <label className={`setup-check${answers.open_to_remote ? " checked" : ""}`} style={{ marginTop: 10 }}>
          <input type="checkbox" checked={answers.open_to_remote}
            onChange={(e) => update({ open_to_remote: e.target.checked })} />
          Open to Remote
        </label>
      </div>

      <div className="setup-field">
        <label className="setup-label">Work Authorization</label>
        {AUTH_QUESTIONS.map(([key, text]) => (
          <div key={key} className="setup-yesno">
            <span className="setup-yesno-q">{text}</span>
            <div className="setup-checkgrid">
              {(["yes", "no"] as const).map((v) => (
                <label key={v} className={`setup-check${answers[key] === v ? " checked" : ""}`}>
                  <input type="radio" name={key} checked={answers[key] === v}
                    onChange={() => update({ [key]: v })} />
                  {v === "yes" ? "Yes" : "No"}
                </label>
              ))}
            </div>
          </div>
        ))}
        <label className={`setup-check${answers.work_authorization.includes("needs_sponsorship") ? " checked" : ""}`}>
          <input type="checkbox" checked={answers.work_authorization.includes("needs_sponsorship")}
            onChange={() => update({ work_authorization: toggle(answers.work_authorization, "needs_sponsorship") })} />
          I will need visa sponsorship
        </label>
      </div>
    </>
  );
}
