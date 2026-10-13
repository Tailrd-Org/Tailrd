import { bandTone, cardFit, type FitFields } from "../lib/fit";

/**
 * The job card's fit badge (lib/fit.ts has the display rules). Words only, never a number:
 * the band for a scored result (the list carries no interval, D-54), "Not eligible" with a
 * "See why" caption, or "We couldn't score this". The detail view has the full result.
 */
export default function FitBadge({ job }: { job: FitFields }) {
  const fit = cardFit(job);
  if (!fit) return null;

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
      {fit.kind === "not-eligible" && <span className="fit-badge-why">See why</span>}
    </div>
  );
}
