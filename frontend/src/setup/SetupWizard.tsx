import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/useAuth";
import api from "../auth/api";
import { useAvailableExperienceLevels } from "../hooks/useAvailableExperienceLevels";
import { SetupLayout } from "./SetupLayout";
import { SETUP_STEPS } from "./setupConfig";
import { ResumeStep } from "./steps/ResumeStep";
import { answersToFilters } from "./answersToFilters";
import { emptyAnswers, type SetupAnswers, type SetupStep } from "./types";

// The appended resume step only needs id + headline: it is rendered specially
// below (via the `step.id === "resume"` check), never through `step.Component`.
type WizardStep = Pick<SetupStep, "id" | "headline"> & Partial<Omit<SetupStep, "id" | "headline">>;

const FILTER_STORAGE_KEY = "job-aggregator-filters";

export default function SetupWizard() {
  const { user, setSetupComplete } = useAuth();
  const navigate = useNavigate();
  const [index, setIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [uploadedResumeId, setUploadedResumeId] = useState<number | null>(null);
  // Asked here, from the first step, so the answer is in long before the experience
  // step shows its options.
  const experienceLevels = useAvailableExperienceLevels();
  const [answers, setAnswers] = useState<SetupAnswers>(() => ({
    ...emptyAnswers,
    first_name: user?.first_name ?? "",
    last_name: user?.last_name ?? "",
  }));

  // Resume is the final step, appended after the config steps.
  const resumeStep: WizardStep = useMemo(
    () => ({ id: "resume", headline: "One last step: <b>level up</b> your search with your resume." }),
    [],
  );
  const steps: WizardStep[] = useMemo(() => [...SETUP_STEPS, resumeStep], [resumeStep]);
  const isLast = index === steps.length - 1;
  const step = steps[index];

  const update = (patch: Partial<SetupAnswers>) => setAnswers((a) => ({ ...a, ...patch }));

  const persist = async () => {
    // 1) Settings (durable). Failure surfaces but does not trap the user.
    try {
      await api.put("/settings", {
        first_name: answers.first_name,
        last_name: answers.last_name,
        // No job_title: it is the CURRENT title the extension types into
        // "Current job title" fields, and a job FUNCTION ("Software
        // Engineering") is not one. The function seeds the feed filters below.
        location: answers.city,
        remote_only: answers.open_to_remote,
        work_type: answers.open_to_remote ? "remote" : "",
        experience_levels: answers.experience_level ? [answers.experience_level] : [],
        regions: answers.country ? [answers.country] : [],
        prefilled_answers: {
          job_types: answers.job_types.join(","),
          work_authorization: answers.work_authorization.join(","),
        },
      });
    } catch {
      /* non-fatal: user can re-save in Settings */
    }
    // 1b) The screening answers the extension fills employer questions from,
    //     through the same endpoint the Profile page and the extension use. Only
    //     what was answered: a blank stays "not answered", never "No".
    const screening: Record<string, string> = {};
    if (answers.authorized_canada) screening.authorizedCanada = answers.authorized_canada === "yes" ? "Yes" : "No";
    if (answers.authorized_us) screening.authorizedUS = answers.authorized_us === "yes" ? "Yes" : "No";
    if (answers.expected_graduation) screening.expectedGraduation = answers.expected_graduation;
    if (answers.work_authorization.includes("needs_sponsorship")) screening.requiresSponsorship = "Yes";
    if (Object.keys(screening).length > 0) {
      try {
        await api.put("/api/user/application-profile", screening);
      } catch {
        /* non-fatal: editable on the Profile page */
      }
    }
    // 2) Resume already uploaded inline via the real /resumes/upload pipeline
    //    on the resume step (required, finish is gated on it), so nothing to do.
    // 3) Seed dashboard filters so first load is personalized.
    try {
      localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify(answersToFilters(answers)));
    } catch {
      /* ignore quota */
    }
  };

  const handleNext = async () => {
    if (step.validate) {
      const msg = step.validate(answers);
      if (msg) { setError(msg); return; }
    }
    setError(null);
    if (!isLast) { setIndex((i) => i + 1); return; }
    // finish
    setSubmitting(true);
    await persist();
    try {
      await setSetupComplete(true);
      navigate("/app");
    } catch {
      setError("Something went wrong finishing setup. Please try again.");
      setSubmitting(false);
    }
  };

  const handleBack = () => { setError(null); setIndex((i) => Math.max(0, i - 1)); };

  return (
    <SetupLayout headline={step.headline} stepIndex={index} total={steps.length}>
      {step.id === "resume"
        ? <ResumeStep uploadedResumeId={uploadedResumeId} onUploaded={setUploadedResumeId} />
        : step.Component && <step.Component answers={answers} update={update} experienceLevels={experienceLevels} />}
      {error && <div className="setup-error" role="alert">{error}</div>}
      <div className="setup-footer">
        {index > 0
          ? <button className="setup-back" onClick={handleBack} disabled={submitting}>Back</button>
          : <span />}
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <button
            className="setup-btn"
            onClick={handleNext}
            disabled={submitting || (isLast && uploadedResumeId === null)}
          >
            {isLast ? (submitting ? "Starting…" : "Start Matching") : "Next"}
          </button>
        </div>
      </div>
    </SetupLayout>
  );
}
