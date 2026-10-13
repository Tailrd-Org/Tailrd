import { bandTone, cardFit, type FitFields } from "../lib/fit";

/**
 * The job card's fit badge (lib/fit.ts has the display rules). SCORED shows "Fit 76" in the
 * ring with the API's band under it; every other outcome is words only, never a number.
 */
export default function FitBadge({ job }: { job: FitFields }) {
  const fit = cardFit(job);
  if (!fit) return null;

  if (fit.kind === "scored") {
    return (
      <div className={`match-score-badge fit-badge fit-${bandTone(fit.band)}`}>
        <div className="match-circle-sm">
          <svg viewBox="0 0 100 100" aria-hidden="true">
            <circle cx="50" cy="50" r="40" fill="none" stroke="#e3e8ee" strokeWidth="8" />
            <circle cx="50" cy="50" r="40" fill="none" stroke="#533afd" strokeWidth="8"
              strokeDasharray={`${Math.max(0, Math.min(100, fit.score)) * 2.51} 251`}
              strokeLinecap="round"
              style={{ transform: "rotate(-90deg)", transformOrigin: "center" }}
            />
          </svg>
          <span className="match-pct fit-score-text">{`Fit ${fit.score}`}</span>
        </div>
        <span className="match-label-sm">{fit.band}</span>
      </div>
    );
  }

  const tone = fit.kind === "band" ? bandTone(fit.band) : "none";
  const text = fit.kind === "band" ? fit.band : fit.label;
  const title =
    fit.kind === "not-eligible"
      ? "Open the job to see the reason"
      : fit.kind === "unscored"
        ? "Open the job to see why"
        : undefined;
  return (
    <div className={`match-score-badge fit-badge fit-${tone} fit-badge-text`}>
      <span className="match-label-sm" title={title}>{text}</span>
    </div>
  );
}
