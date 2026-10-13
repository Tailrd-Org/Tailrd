import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  BellRinging,
  Buildings,
  CheckCircle,
  FileText,
  HourglassMedium,
  Lightning,
  Minus,
  Plus,
  PuzzlePiece,
  Sparkle,
  Target,
} from "@phosphor-icons/react";
import CompanyLogo from "./CompanyLogo";
import { pingExtension, type ExtensionState } from "../lib/extensionBridge";
import { CHROME_STORE_URL } from "../lib/extensionStore";
import { skillLabel } from "../lib/skillLabel";
import { bandFor, FIT_BANDS, STRONG_MIN } from "../lib/fit";
import "./jobs-sidebar.css";

export interface SidebarData {
  resume: {
    id: number;
    name: string;
    scored_jobs: number;
    avg_match: number | null;
    strong_matches: number;
    gap_pool_size: number;
  } | null;
  resume_skills: string[];
  skill_gaps: { skill: string; job_count: number }[];
  progress: {
    week_start: string;
    applied_week: number;
    applied_total: number;
    saved_week: number;
    interviews: number;
  } | null;
  closing_soon: {
    id: number;
    title: string;
    company: string;
    company_logo: string;
    company_domain: string;
    company_url: string;
    age_days: number | null;
    reason: "stale" | "old";
  }[];
  feed: {
    total: number;
    new_since: number;
    since: string;
    remote: number;
    strong_matches: number | null;
  };
  top_companies: {
    company: string;
    count: number;
    company_logo: string;
    company_domain: string;
    company_url: string;
  }[];
  top_companies_basis: "matches" | "open";
  autofill: { fields_filled: number; passes: number } | null;
  alerts_enabled: boolean | null;
}

interface Props {
  data: SidebarData | null;
  onShowStrong: () => void;
  onShowNew: (since: string) => void;
  onShowRemote: () => void;
  onCompany: (company: string) => void;
  onOpenJob: (jobId: number) => void;
  onAlertsChange: (enabled: boolean) => Promise<void>;
}

const GOAL_KEY = "tailrd.jobs.weeklyGoal";
const DEFAULT_GOAL = 10;
// Alerts go out for confident Strong matches only (D-85), named by the engine's band.
const STRONG_LABEL = `${FIT_BANDS[0].band} (fit score ${STRONG_MIN} or higher)`;

function readGoal(): number {
  try {
    const n = Number(localStorage.getItem(GOAL_KEY));
    return Number.isInteger(n) && n >= 1 && n <= 50 ? n : DEFAULT_GOAL;
  } catch {
    return DEFAULT_GOAL;
  }
}

function Ring({ value, max, label }: { value: number; max: number; label: string }) {
  const pct = max > 0 ? Math.min(1, value / max) : 0;
  return (
    <div className="rail-ring" role="img" aria-label={label}>
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <circle cx="50" cy="50" r="42" className="rail-ring-track" />
        <circle
          cx="50"
          cy="50"
          r="42"
          className="rail-ring-fill"
          strokeDasharray={`${pct * 263.9} 263.9`}
        />
      </svg>
      <span className="rail-ring-value">{value}</span>
    </div>
  );
}

function ResumeCard({ data }: { data: SidebarData }) {
  const { resume, skill_gaps } = data;
  if (!resume) {
    return (
      <section className="rail-card" aria-labelledby="rail-resume">
        <h3 id="rail-resume" className="rail-title">
          <FileText size={16} weight="duotone" /> Résumé match
        </h3>
        <p className="rail-muted">Upload a résumé to see how well you fit each job.</p>
        <Link to="/app/resume" className="rail-link">
          Upload résumé <ArrowRight size={13} weight="bold" />
        </Link>
      </section>
    );
  }
  return (
    <section className="rail-card" aria-labelledby="rail-resume">
      <h3 id="rail-resume" className="rail-title">
        <FileText size={16} weight="duotone" /> Résumé match
      </h3>
      <p className="rail-resume-name" title={resume.name}>{resume.name}</p>
      {resume.scored_jobs > 0 && resume.avg_match !== null ? (
        <div className="rail-stat-row">
          <Ring value={resume.avg_match} max={100} label={`Average fit score ${resume.avg_match}`} />
          <p className="rail-stat-copy">
            <strong>{resume.avg_match} average fit score</strong> ({bandFor(resume.avg_match)}) across{" "}
            {resume.scored_jobs} scored {resume.scored_jobs === 1 ? "job" : "jobs"}
            {resume.strong_matches > 0 && <>, {resume.strong_matches} strong</>}
          </p>
        </div>
      ) : (
        <p className="rail-muted">We're still scoring jobs against this résumé. Check back soon.</p>
      )}
      {skill_gaps.length > 0 && (
        <div className="rail-gaps">
          <p className="rail-label">Often asked for, not on your résumé</p>
          <ul className="rail-chips">
            {skill_gaps.map((g) => (
              <li key={g.skill} className="rail-chip" title={`Listed in ${g.job_count} jobs that fit you`}>
                {skillLabel(g.skill)} <span className="rail-chip-count">{g.job_count}</span>
              </li>
            ))}
          </ul>
          <p className="rail-hint">Only add the ones you've actually used.</p>
        </div>
      )}
      <Link to={`/app/resume/${resume.id}`} className="rail-link">
        Review résumé <ArrowRight size={13} weight="bold" />
      </Link>
    </section>
  );
}

function ProgressCard({ progress }: { progress: NonNullable<SidebarData["progress"]> }) {
  const [goal, setGoal] = useState(readGoal);
  const setAndStore = (next: number) => {
    const g = Math.max(1, Math.min(50, next));
    setGoal(g);
    try {
      localStorage.setItem(GOAL_KEY, String(g));
    } catch {
      // Private window or blocked storage: the goal just won't persist.
    }
  };
  const done = progress.applied_week >= goal;
  return (
    <section className="rail-card" aria-labelledby="rail-progress">
      <h3 id="rail-progress" className="rail-title">
        <Target size={16} weight="duotone" /> This week
      </h3>
      <div className="rail-stat-row">
        <Ring
          value={progress.applied_week}
          max={goal}
          label={`${progress.applied_week} of ${goal} applications this week`}
        />
        <div className="rail-stat-copy">
          <strong>
            {progress.applied_week} of {goal} applications
          </strong>
          <span className="rail-muted">
            {done ? "Goal reached. Nice work." : `${goal - progress.applied_week} to go`}
          </span>
        </div>
      </div>
      <div className="rail-goal">
        <span className="rail-label">Weekly goal</span>
        <div className="rail-stepper">
          <button type="button" onClick={() => setAndStore(goal - 1)} aria-label="Lower weekly goal" disabled={goal <= 1}>
            <Minus size={12} weight="bold" />
          </button>
          <span aria-live="polite">{goal}</span>
          <button type="button" onClick={() => setAndStore(goal + 1)} aria-label="Raise weekly goal" disabled={goal >= 50}>
            <Plus size={12} weight="bold" />
          </button>
        </div>
      </div>
      <dl className="rail-mini-stats">
        <div>
          <dt>Saved</dt>
          <dd>{progress.saved_week}</dd>
        </div>
        <div>
          <dt>Interviews</dt>
          <dd>{progress.interviews}</dd>
        </div>
        <div>
          <dt>All time</dt>
          <dd>{progress.applied_total}</dd>
        </div>
      </dl>
      <Link to="/app/applications" className="rail-link">
        View applications <ArrowRight size={13} weight="bold" />
      </Link>
    </section>
  );
}

/** Dev-only, same reason as ExtensionBanner: the ping can't reach localhost. */
function devStateOverride(): ExtensionState | null {
  if (!import.meta.env.DEV) return null;
  const v = new URLSearchParams(window.location.search).get("extState");
  return v === "connected" || v === "installed" || v === "not-installed" ? v : null;
}

function ExtensionCard({ autofill }: { autofill: SidebarData["autofill"] }) {
  const [state, setState] = useState<ExtensionState>("unknown");
  useEffect(() => {
    const override = devStateOverride();
    if (override) {
      setState(override);
      return;
    }
    let alive = true;
    void pingExtension()
      .then((s) => {
        if (alive) setState(s);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // Render nothing until the ping answers, so a user who has the extension
  // never sees an install prompt flash.
  if (state === "unknown") return null;

  const fields = autofill?.fields_filled ?? 0;
  if (state === "connected") {
    return (
      <section className="rail-card rail-card-ok" aria-labelledby="rail-ext">
        <h3 id="rail-ext" className="rail-title">
          <CheckCircle size={16} weight="fill" /> Autofill is ready
        </h3>
        <p className="rail-muted">
          {fields > 0
            ? `Tailrd has filled ${fields.toLocaleString()} application ${fields === 1 ? "field" : "fields"} for you.`
            : "Open any application and Tailrd fills it from your profile. You review before submitting."}
        </p>
      </section>
    );
  }
  const installed = state === "installed";
  return (
    <section className="rail-card rail-card-promo" aria-labelledby="rail-ext">
      <h3 id="rail-ext" className="rail-title">
        <PuzzlePiece size={16} weight="duotone" />
        {installed ? "Finish connecting Tailrd" : "Apply faster with Tailrd"}
      </h3>
      <p className="rail-muted">
        {installed
          ? "Sign in to the extension so autofill can use your profile and résumé."
          : "The Chrome extension fills application forms from your profile. You check every answer."}
      </p>
      {installed ? (
        <Link to="/extension/connect" className="rail-btn">
          Connect extension
        </Link>
      ) : (
        <a href={CHROME_STORE_URL} target="_blank" rel="noopener noreferrer" className="rail-btn">
          Add to Chrome
        </a>
      )}
    </section>
  );
}

function ClosingCard({ items, onOpenJob }: { items: SidebarData["closing_soon"]; onOpenJob: (id: number) => void }) {
  if (items.length === 0) return null;
  return (
    <section className="rail-card" aria-labelledby="rail-closing">
      <h3 id="rail-closing" className="rail-title">
        <HourglassMedium size={16} weight="duotone" /> Apply before these close
      </h3>
      <ul className="rail-list">
        {items.map((job) => (
          <li key={job.id}>
            <button type="button" className="rail-job" onClick={() => onOpenJob(job.id)}>
              <CompanyLogo
                company={job.company}
                company_logo={job.company_logo}
                company_domain={job.company_domain}
                company_url={job.company_url}
                size={28}
              />
              <span className="rail-job-text">
                <span className="rail-job-title">{job.title}</span>
                <span className="rail-job-meta">
                  {job.company} ·{" "}
                  <span className={job.reason === "stale" ? "rail-warn" : undefined}>
                    {job.reason === "stale"
                      ? "may be closing"
                      : `posted ${job.age_days}d ago`}
                  </span>
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function FeedCard({
  feed,
  onShowStrong,
  onShowNew,
  onShowRemote,
}: {
  feed: SidebarData["feed"];
  onShowStrong: () => void;
  onShowNew: (since: string) => void;
  onShowRemote: () => void;
}) {
  return (
    <section className="rail-card" aria-labelledby="rail-feed">
      <h3 id="rail-feed" className="rail-title">
        <Lightning size={16} weight="duotone" /> Your feed
      </h3>
      <div className="rail-pulse">
        <button type="button" onClick={() => onShowNew(feed.since)} disabled={feed.new_since === 0}>
          <strong>{feed.new_since.toLocaleString()}</strong>
          <span>new since last visit</span>
        </button>
        {feed.strong_matches !== null && (
          <button type="button" onClick={onShowStrong} disabled={feed.strong_matches === 0}>
            <strong>{feed.strong_matches.toLocaleString()}</strong>
            <span>strong matches</span>
          </button>
        )}
        <button type="button" onClick={onShowRemote} disabled={feed.remote === 0}>
          <strong>{feed.remote.toLocaleString()}</strong>
          <span>remote</span>
        </button>
      </div>
      <p className="rail-hint">{feed.total.toLocaleString()} open jobs in total</p>
    </section>
  );
}

function CompaniesCard({
  companies,
  basis,
  onCompany,
}: {
  companies: SidebarData["top_companies"];
  basis: SidebarData["top_companies_basis"];
  onCompany: (company: string) => void;
}) {
  if (companies.length === 0) return null;
  return (
    <section className="rail-card" aria-labelledby="rail-companies">
      <h3 id="rail-companies" className="rail-title">
        <Buildings size={16} weight="duotone" />
        {basis === "matches" ? "Hiring people like you" : "Hiring the most"}
      </h3>
      <ul className="rail-companies">
        {companies.map((c) => (
          <li key={c.company}>
            <button
              type="button"
              className="rail-company"
              onClick={() => onCompany(c.company)}
              title={`Show ${c.company} jobs`}
            >
              <CompanyLogo
                company={c.company}
                company_logo={c.company_logo}
                company_domain={c.company_domain}
                company_url={c.company_url}
                size={32}
              />
              <span className="rail-company-name">{c.company}</span>
              <span className="rail-company-count">
                {c.count} {basis === "matches" ? (c.count === 1 ? "fit" : "fits") : "open"}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function AlertsCard({ enabled, onChange }: { enabled: boolean; onChange: (v: boolean) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const toggle = async () => {
    setBusy(true);
    setError(false);
    try {
      await onChange(!enabled);
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="rail-card" aria-labelledby="rail-alerts">
      <div className="rail-alerts-row">
        <h3 id="rail-alerts" className="rail-title">
          <BellRinging size={16} weight="duotone" /> Strong-match emails
        </h3>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-labelledby="rail-alerts"
          className={`rail-switch${enabled ? " on" : ""}`}
          onClick={toggle}
          disabled={busy}
        >
          <span className="rail-switch-knob" />
        </button>
      </div>
      <p className="rail-muted">
        {enabled
          ? `We'll email you when a new job is a ${STRONG_LABEL} against your résumé.`
          : `Turn on to get an email when a new job is a ${STRONG_LABEL}.`}
      </p>
      {error && <p className="rail-error" role="alert">Couldn't update. Try again.</p>}
    </section>
  );
}

export default function JobsSidebar({
  data,
  onShowStrong,
  onShowNew,
  onShowRemote,
  onCompany,
  onOpenJob,
  onAlertsChange,
}: Props) {
  if (!data) {
    return (
      <aside className="jobs-rail" aria-label="Your job search" aria-busy="true">
        <div className="rail-card rail-skeleton" />
        <div className="rail-card rail-skeleton" />
        <div className="rail-card rail-skeleton short" />
      </aside>
    );
  }
  return (
    <aside className="jobs-rail" aria-label="Your job search">
      <ResumeCard data={data} />
      {data.progress && <ProgressCard progress={data.progress} />}
      <ExtensionCard autofill={data.autofill} />
      <ClosingCard items={data.closing_soon} onOpenJob={onOpenJob} />
      <FeedCard
        feed={data.feed}
        onShowStrong={onShowStrong}
        onShowNew={onShowNew}
        onShowRemote={onShowRemote}
      />
      <CompaniesCard companies={data.top_companies} basis={data.top_companies_basis} onCompany={onCompany} />
      {data.alerts_enabled !== null && (
        <AlertsCard enabled={data.alerts_enabled} onChange={onAlertsChange} />
      )}
      <p className="rail-footnote">
        <Sparkle size={12} weight="fill" /> Fit scores compare your résumé with each posting. Use them as a guide, not a verdict.
      </p>
    </aside>
  );
}
