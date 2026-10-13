import { useState, useEffect, useRef } from "react";
import CompanyLogo from "./CompanyLogo";
import { displayLocation } from "../lib/jobLocation";
import { getPrimaryResumeText, skillCovered } from "../lib/resumeCoverage";
import {
  checkListingLive,
  handleApplyLinkClick,
  isListingClosed,
  listingStatusFromLiveCheck,
  postedAgo,
  sourceLabel,
} from "../lib/jobListing";
import {
  X,
  MapPin,
  House,
  Flag,
  GraduationCap,
  CurrencyDollar,
  GithubLogo,
  LinkedinLogo,
  PaperPlaneTilt,
  ArrowSquareOut,
  Prohibit,
  Users,
  ThumbsUp,
  ClipboardText,
  Star,
  Gift,
  Buildings,
  Code,
  ListBullets,
  Info,
  FileText,
  type Icon,
} from "@phosphor-icons/react";
import api from "../auth/api";
import { bandTone, cardFit, fitSummary, impactViews, type BandTone, type ImpactView } from "../lib/fit";
import { useApplyTracking } from "../context/ApplyTracking";

// Maps the client-side parser's icon keys to Phosphor icon components
const SECTION_ICONS: Record<string, Icon> = {
  "clipboard-list": ClipboardText,
  "graduation-cap": GraduationCap,
  star: Star,
  gift: Gift,
  building: Buildings,
  code: Code,
  list: ListBullets,
  "info-circle": Info,
};

interface MatchItem {
  label: string;
  impact?: string;
  note?: string;
  code?: string;
}

/** POST /ai/match-breakdown: the deterministic fit engine's result (backend/schemas/match.py). */
interface MatchBreakdown {
  outcome: string; // SCORED | SCORED_LOW_CONFIDENCE | NEEDS_REVIEW | GATE_FAIL
  band?: string | null;
  display_score?: number | null; // set only for SCORED
  headline?: string;
  basis?: string;
  strength_items?: MatchItem[];
  gaps?: MatchItem[];
  to_improve?: string[];
  notes?: string[];
  interval?: { low: number; high: number; display: boolean } | null;
}

interface Job {
  id: number;
  title: string;
  company: string;
  location: string;
  url: string;
  description: string;
  // The engine's result on the card: a number only when match_outcome is SCORED.
  match_score: number;
  match_band?: string | null;
  match_outcome?: string | null;
  match_label: string;
  experience_score: number;
  skill_score: number;
  industry_score: number;
  applicant_count: number | null;
  source_platform: string;
  ats_type?: string;
  scraped_at: string;
  salary_range: string;
  status: string;
  listing_status?: string | null;
  company_logo?: string;
  company_domain?: string;
  company_url?: string;
  work_type?: string;
  role_category?: string;
  country?: string;
  experience_level?: string;
  posted_date?: string | null;
  locations_json?: { city?: string; region?: string; region_name?: string; country?: string }[] | null;
}

const TONE_COLORS: Record<BandTone, string> = {
  strong: "#533afd",
  good: "#4f7cff",
  partial: "#f59e0b",
  limited: "#64748d",
  none: "#64748d",
};

/** What the page needs to update the job's card after a breakdown (Jobs.tsx). */
export interface FitChange {
  match_outcome: string;
  match_band: string | null;
  match_score: number;
}

function Impact({ view }: { view: ImpactView }) {
  const tone = view.word.toLowerCase().includes("plus") ? "plus" : "gap";
  return (
    <span className={`fit-impact fit-impact-${tone}`} title={view.raw} aria-label={`${view.word} (${view.raw})`}>
      <span className="fit-impact-bar" aria-hidden="true">
        <span className="fit-impact-fill" style={{ width: `${view.width}%` }} />
      </span>
      <span aria-hidden="true">{view.word}</span>
    </span>
  );
}

function FitItems({ title, items, impacts }: { title: string; items: MatchItem[]; impacts: Array<ImpactView | null> | null }) {
  if (items.length === 0) return null;
  return (
    <div className="fit-section">
      <h4 className="fit-section-title">{title}</h4>
      <ul className="fit-items">
        {items.map((item, i) => (
          <li key={`${item.code || item.label}-${i}`} className="fit-item">
            <span className="fit-item-row">
              <span className="fit-item-label">{item.label}</span>
              {impacts?.[i] && <Impact view={impacts[i]!} />}
            </span>
            {item.note && <span className="fit-item-note">{item.note}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function FitTexts({ title, items, className }: { title: string; items: string[]; className?: string }) {
  if (items.length === 0) return null;
  return (
    <div className={`fit-section${className ? ` ${className}` : ""}`}>
      <h4 className="fit-section-title">{title}</h4>
      <ul className="fit-texts">
        {items.map((text, i) => (
          <li key={i}>{text}</li>
        ))}
      </ul>
    </div>
  );
}

function formatWorkType(wt: string): string {
  if (wt === "remote") return "Remote";
  if (wt === "hybrid") return "Hybrid";
  if (wt === "onsite") return "On Site";
  return wt;
}

function formatCountry(c: string): string {
  if (c === "US") return "USA";
  if (c === "CA") return "Canada";
  return c;
}

function formatExperienceLevel(level: string): string {
  if (level === "new_grad") return "New Grad";
  if (level === "internship") return "Internship";
  return level;
}

interface Props {
  job: Job;
  onClose?: () => void;
  /** Called when the click-time live check changes the listing's status. */
  onListingStatusChange?: (jobId: number, listingStatus: string) => void;
  /** Called with the engine's result once the breakdown answers, so the card can match it. */
  onFitChange?: (jobId: number, fit: FitChange) => void;
}

/**
 * Fast client-side parser that structures job descriptions into sections
 * using regex/heuristics. No AI call needed, instant results.
 */
function parseDescriptionClientSide(rawDesc: string): any | null {
  // Clean HTML tags and decode entities
  let text = rawDesc
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/?(p|div|li|ul|ol|h[1-6])[^>]*>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n");

  // Section header patterns
  const sectionPatterns = [
    { regex: /(?:^|\n)\s*(?:key\s+)?responsibilities?\s*[:\-]?\s*\n/i, title: "Responsibilities", icon: "clipboard-list" },
    { regex: /(?:^|\n)\s*(?:what you(?:'ll| will) do|your role|the role|about the role|job duties|duties)\s*[:\-]?\s*\n/i, title: "Responsibilities", icon: "clipboard-list" },
    { regex: /(?:^|\n)\s*(?:requirements?|qualifications?|what we(?:'re| are) looking for|who you are|must have|minimum qualifications?)\s*[:\-]?\s*\n/i, title: "Qualifications", icon: "graduation-cap" },
    { regex: /(?:^|\n)\s*(?:required skills?|required experience|basic qualifications?)\s*[:\-]?\s*\n/i, title: "Required Qualifications", icon: "graduation-cap" },
    { regex: /(?:^|\n)\s*(?:preferred|nice to have|bonus|preferred qualifications?|desired)\s*[:\-]?\s*\n/i, title: "Preferred Qualifications", icon: "star" },
    { regex: /(?:^|\n)\s*(?:benefits?|perks?|what we offer|compensation|why join us|why work here)\s*[:\-]?\s*\n/i, title: "Benefits", icon: "gift" },
    { regex: /(?:^|\n)\s*(?:about (?:us|the (?:company|team))|who we are|company overview)\s*[:\-]?\s*\n/i, title: "About the Company", icon: "building" },
    { regex: /(?:^|\n)\s*(?:tech(?:nology)? stack|technologies|tools)\s*[:\-]?\s*\n/i, title: "Tech Stack", icon: "code" },
  ];

  // Find all section boundaries
  const sections: { title: string; icon: string; start: number; end?: number }[] = [];

  for (const pattern of sectionPatterns) {
    const match = text.match(pattern.regex);
    if (match && match.index !== undefined) {
      const start = match.index + match[0].length;
      // Avoid duplicate section titles
      if (!sections.find(s => s.title === pattern.title && Math.abs(s.start - start) < 50)) {
        sections.push({ title: pattern.title, icon: pattern.icon, start });
      }
    }
  }

  // If we found fewer than 2 sections, try a simpler bullet-point based approach
  if (sections.length < 2) {
    // Try to split by bullet points into a single "Description" section
    const bullets = text.split("\n")
      .map(l => l.replace(/^[\s•\-\*·▪►●○◦‣⁃]+/, "").trim())
      .filter(l => l.length > 10 && l.length < 300);

    if (bullets.length >= 3) {
      // Extract skills from the text
      const skills = extractSkills(text);
      return {
        sections: [{ title: "Job Description", icon: "list", items: bullets.slice(0, 20) }],
        skills,
      };
    }
    return null; // Can't parse, show raw
  }

  // Sort sections by position
  sections.sort((a, b) => a.start - b.start);

  // Set end boundaries
  for (let i = 0; i < sections.length; i++) {
    sections[i].end = i < sections.length - 1 ? sections[i + 1].start : text.length;
  }

  // Extract items from each section
  const result: any[] = [];
  for (const section of sections) {
    const sectionText = text.slice(section.start, section.end);
    const items = sectionText.split("\n")
      .map(l => l.replace(/^[\s•\-\*·▪►●○◦‣⁃\d.)+]+/, "").trim())
      .filter(l => l.length > 5 && l.length < 500);

    if (items.length > 0) {
      result.push({ title: section.title, icon: section.icon, items: items.slice(0, 15) });
    }
  }

  if (result.length === 0) return null;

  // Also add intro text (before first section) if meaningful
  const introEnd = sections[0].start;
  if (introEnd > 50) {
    const introText = text.slice(0, introEnd).trim();
    const introLines = introText.split("\n").map(l => l.trim()).filter(l => l.length > 10);
    if (introLines.length > 0 && introLines.length <= 5) {
      result.unshift({ title: "Overview", icon: "info-circle", items: introLines });
    }
  }

  const skills = extractSkills(text);
  return { sections: result, skills };
}

function extractSkills(text: string): string[] {
  const knownSkills = [
    "Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Go", "Rust", "Ruby", "PHP", "Swift", "Kotlin",
    "React", "Angular", "Vue", "Node.js", "Django", "Flask", "Spring", "Express", "Next.js",
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "Jenkins", "CI/CD",
    "SQL", "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch", "DynamoDB", "Cassandra",
    "REST", "GraphQL", "gRPC", "Microservices", "Kafka", "RabbitMQ",
    "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch", "NLP",
    "Git", "Linux", "Agile", "Scrum", "Jira",
    "HTML", "CSS", "Tailwind", "SASS",
    "Figma", "Sketch",
    "Spark", "Hadoop", "Airflow", "dbt", "Snowflake", "BigQuery",
    "iOS", "Android", "React Native", "Flutter",
  ];

  const found: string[] = [];
  const lowerText = text.toLowerCase();
  for (const skill of knownSkills) {
    if (lowerText.includes(skill.toLowerCase())) {
      found.push(skill);
    }
    if (found.length >= 12) break;
  }
  return found;
}

export default function JobDetailView({ job, onClose, onListingStatusChange, onFitChange }: Props) {
  const [breakdown, setBreakdown] = useState<MatchBreakdown | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [applyUrl, setApplyUrl] = useState(job.url);
  const [description, setDescription] = useState(job.description || "");
  const [companyLogo, setCompanyLogo] = useState(job.company_logo || "");
  const [fetchingDetails, setFetchingDetails] = useState(false);
  const [structured, setStructured] = useState<any>(null);
  const [resumeText, setResumeText] = useState("");
  // listing_status from the click-time live check; null until it answers.
  const [liveStatus, setLiveStatus] = useState<string | null>(null);
  const { registerApplyClick } = useApplyTracking();
  // The job on screen. The parent reuses this panel across jobs, so a request
  // started for an earlier job must not write its answer onto this one.
  const activeJobIdRef = useRef(job.id);
  activeJobIdRef.current = job.id;

  // Primary resume text (session-cached) powers the qualification-tag
  // coverage highlighting; no resume just means neutral tags.
  useEffect(() => {
    let cancelled = false;
    getPrimaryResumeText().then((text) => {
      if (!cancelled) setResumeText(text);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    setApplyUrl(job.url);
    setDescription(job.description || "");
    setCompanyLogo(job.company_logo || "");
    setBreakdown(null);
    setError("");
    setStructured(null);
    // A previous job's fetch or analysis may still be running; its spinner
    // is not this job's.
    setFetchingDetails(false);
    setLoading(false);

    if (job.description && job.description.length > 50) {
      const parsed = parseDescriptionClientSide(job.description);
      if (parsed) setStructured(parsed);
    }

    async function fetchStructured(effectiveDescription: string) {
      if (effectiveDescription.length <= 50) return;
      try {
        const { data } = await api.post(`/jobs/${job.id}/structure-description`);
        if (cancelled) return;
        if (data && Array.isArray(data.sections) && data.sections.length > 0) {
          setStructured(data);
        }
      } catch {
        // keep the instant client-side parse
      }
    }

    (async () => {
      // Details first: fetch-details backfills an empty description, and the
      // engine reads the posting from it. Asked before, an empty posting
      // would always read "We couldn't score this".
      const fetchedDescription = await fetchJobDetails();
      if (cancelled) return;

      const effectiveDescription = fetchedDescription || job.description || "";
      if (effectiveDescription.length > 50) {
        const parsed = parseDescriptionClientSide(effectiveDescription);
        if (parsed) setStructured(parsed);
      }

      // Upgrade to the cached server-side structured parse (Responsibilities /
      // Required / Preferred / skills) once it's available.
      fetchStructured(effectiveDescription);

      // The breakdown is deterministic and free now (no AI), so every open
      // asks for it: the card only carries the band and the number.
      triggerAnalysis();
    })();

    return () => {
      cancelled = true;
    };
  }, [job.id]);

  // Click-time liveness: re-verify the listing against its source in the
  // background (once per job per session). Apply stays usable while it runs
  // (a click waits briefly for the answer, see onApplyClick); a dead answer
  // switches the panel to the closed state.
  useEffect(() => {
    let cancelled = false;
    setLiveStatus(null);
    const knownStatus = job.listing_status || "active";
    checkListingLive(job.id).then((result) => {
      if (!result) return;
      const status = listingStatusFromLiveCheck(result);
      if (!cancelled) setLiveStatus(status);
      if (status !== knownStatus) onListingStatusChange?.(job.id, status);
    });
    return () => {
      cancelled = true;
    };
  }, [job.id]);

  useEffect(() => {
    if (description && description.length > 50 && !structured) {
      const parsed = parseDescriptionClientSide(description);
      if (parsed) setStructured(parsed);
    }
  }, [description]);

  // Always asked, even with a long local description: it may be a placeholder that
  // fetch-details replaces, and the breakdown must score the real posting. The
  // backend answers at once when it already has a good description. The spinner
  // only replaces the text when there is none to show yet.
  async function fetchJobDetails(): Promise<string> {
    const local = job.description && job.description.length > 50 ? job.description : "";
    const requestedId = job.id;
    const stillOnScreen = () => activeJobIdRef.current === requestedId;
    if (!local) setFetchingDetails(true);
    try {
      const { data } = await api.post(`/jobs/${requestedId}/fetch-details`);
      // The fetch found the posting closed (404, error landing, closed
      // notice): the backend already marked it removed, so show it closed.
      // The feed hears about it even when another job is on screen now.
      if (data.dead) {
        const status = data.listing_status || "removed";
        onListingStatusChange?.(requestedId, status);
        if (stillOnScreen()) setLiveStatus(status);
      }
      if (!stillOnScreen()) return "";
      if (data?.apply_url) setApplyUrl(data.apply_url);
      if (data?.description && data.description !== local) setDescription(data.description);
      if (data?.company_logo) setCompanyLogo(data.company_logo);
      return data?.description || local;
    } catch {
      // Keep original URL if fetch fails
    } finally {
      if (stillOnScreen()) setFetchingDetails(false);
    }
    return local;
  }

  async function triggerAnalysis() {
    const requestedId = job.id;
    const stillOnScreen = () => activeJobIdRef.current === requestedId;
    setLoading(true);
    setError("");
    try {
      const { data } = await api.post<MatchBreakdown>(`/ai/match-breakdown/${requestedId}`);
      if (data && typeof data.outcome === "string") {
        // The breakdown banks this result, so the card takes it even if the
        // user has moved on to another job.
        onFitChange?.(requestedId, {
          match_outcome: data.outcome,
          match_band: data.band ?? null,
          match_score: data.outcome === "SCORED" ? data.display_score ?? 0 : 0,
        });
        if (stillOnScreen()) setBreakdown(data);
      }
    } catch (err: unknown) {
      if (!stillOnScreen()) return;
      const status = (err as { response?: { status?: number } })?.response?.status;
      if (status === 503) {
        // Nothing for the user to connect or configure; it is on our side.
        setError("Match scoring is temporarily unavailable. Please try again later.");
      } else if (status === 400) {
        setError("Upload a résumé to see how you fit this job.");
      } else if (status) {
        setError("We couldn't load your fit for this job. Please try again later.");
      } else {
        setError("Failed to connect to the server.");
      }
    } finally {
      if (stillOnScreen()) setLoading(false);
    }
  }

  // Apply while the live check is still out waits briefly for it, so a dead
  // listing closes here instead of opening its dead link.
  function onApplyClick(event: Parameters<typeof handleApplyLinkClick>[0]) {
    const requestedId = job.id;
    const url = applyUrl;
    handleApplyLinkClick(event, requestedId, url, {
      onOpened: () => registerApplyClick({ id: requestedId, title: job.title, company: job.company, url }),
      onClosed: (status) => {
        if (activeJobIdRef.current === requestedId) setLiveStatus(status);
      },
    });
  }

  const closed = isListingClosed(liveStatus ?? job.listing_status);
  const source = sourceLabel(job);
  // The full result once the breakdown answers; until then, the card's.
  const cardResult = cardFit(job);
  // Only the breakdown shows a number (the card's may hide a displayed interval, D-54).
  const scored = !!breakdown && breakdown.outcome === "SCORED" && typeof breakdown.display_score === "number";
  const score = scored ? breakdown!.display_score! : 0;
  const headline = breakdown ? breakdown.headline || "" : cardResult ? fitSummary(cardResult) : "";
  const band = breakdown ? breakdown.band : cardResult?.kind === "band" ? cardResult.band : null;
  const color = TONE_COLORS[bandTone(band)];
  // A displayed interval replaces the number everywhere (D-54): the ring shows the range.
  const interval = scored && breakdown?.interval?.display ? breakdown.interval : null;
  const strengthItems = breakdown?.strength_items ?? [];
  const gapItems = breakdown?.gaps ?? [];
  // Impacts are shown on SCORED results only, as words sized against the largest one.
  const impacts = scored ? impactViews([...strengthItems, ...gapItems].map((item) => item.impact)) : null;

  return (
    <div className="job-detail-view">
      {/* Close button */}
      {onClose && (
        <button className="btn-close-detail" onClick={onClose} aria-label="Close detail panel">
          <X size={18} weight="bold" />
        </button>
      )}

      {/* Header section */}
      <div className="job-detail-header-section">
        <div className="job-detail-company-row">
          <CompanyLogo
            company={job.company}
            company_logo={companyLogo || job.company_logo}
            company_domain={job.company_domain}
            company_url={job.company_url}
            size={52}
          />
          <div className="detail-company-info">
            <span className="job-detail-company">{job.company}</span>
            <span className="job-detail-posted">{postedAgo(job.posted_date || job.scraped_at)}</span>
          </div>
        </div>

        <h1 className="job-detail-title">{job.title}</h1>

        {closed && (
          <p className="listing-closed-badge" role="status">
            <Prohibit size={14} weight="bold" /> No longer accepting applications
          </p>
        )}

        {/* Tags row */}
        <div className="job-detail-tags">
          {job.location && (
            <span
              className="detail-tag"
              title={(job.locations_json?.length ?? 0) > 1 ? job.location : undefined}
            >
              <MapPin size={14} weight="duotone" /> {displayLocation(job) || job.location}
            </span>
          )}
          {job.work_type && (
            <span className="detail-tag">
              <House size={14} weight="duotone" /> {formatWorkType(job.work_type)}
            </span>
          )}
          {job.country && (
            <span className="detail-tag">
              <Flag size={14} weight="duotone" /> {formatCountry(job.country)}
            </span>
          )}
          {job.experience_level && (
            <span className="detail-tag detail-tag-highlight">
              <GraduationCap size={14} weight="duotone" /> {formatExperienceLevel(job.experience_level)}
            </span>
          )}
          {job.salary_range && (
            <span className="detail-tag detail-tag-salary">
              <CurrencyDollar size={14} weight="duotone" /> {job.salary_range}
            </span>
          )}
          {job.role_category && (
            <span className="detail-tag detail-tag-category">
              {job.role_category}
            </span>
          )}
          {job.source_platform && (
            <span className="detail-tag">
              {source.kind === "github" ? (
                <GithubLogo size={14} weight="fill" />
              ) : source.kind === "linkedin" ? (
                <LinkedinLogo size={14} weight="fill" />
              ) : (
                <Buildings size={14} weight="duotone" />
              )}{" "}
              {source.label}
            </span>
          )}
        </div>

        {/* Action buttons */}
        <div className="job-detail-actions">
          {closed ? (
            // A closed listing's link is dead (or lands on a careers home
            // page): keep the actions in place but never navigate.
            <>
              <button type="button" className="btn-apply-detail" disabled>
                <PaperPlaneTilt size={16} weight="fill" /> Apply with Tailrd
              </button>
              <button type="button" className="btn-outline-detail" disabled>
                <ArrowSquareOut size={16} weight="bold" /> View Original Post
              </button>
            </>
          ) : (
            <>
              <a
                href={applyUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="btn-apply-detail"
                onClick={onApplyClick}
              >
                <PaperPlaneTilt size={16} weight="fill" /> Apply with Tailrd
              </a>
              <a
                href={applyUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="btn-outline-detail"
                onClick={onApplyClick}
              >
                <ArrowSquareOut size={16} weight="bold" /> View Original Post
              </a>
            </>
          )}
        </div>
      </div>

      {/* Content area */}
      <div className="job-detail-content">
        {/* Main content */}
        <div className="job-detail-main">
          <div className="job-detail-description">
            <h2 className="detail-section-title">Overview</h2>
            {fetchingDetails ? (
              <div className="description-loading">
                <div className="spinner" />
                <span>Loading job details...</span>
              </div>
            ) : structured ? (
              <div className="structured-description">
                {/* Qualification skill tags, highlighted by resume coverage */}
                {structured.skills && structured.skills.length > 0 && (
                  <div className="qual-tags-block">
                    {resumeText && (
                      <div className="qual-tags-caption">
                        <ThumbsUp size={12} weight="fill" /> marks skills your resume already covers
                      </div>
                    )}
                    <div className="skill-tags">
                      {structured.skills.map((skill: string, i: number) => {
                        const matched = resumeText ? skillCovered(resumeText, skill) : false;
                        return (
                          <span
                            key={i}
                            className={`skill-tag${matched ? " skill-tag-matched" : ""}`}
                          >
                            {matched && <ThumbsUp size={12} weight="fill" />}
                            {skill}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                )}
                {/* Sections */}
                {structured.sections.map((section: any, i: number) => {
                  const SectionIcon = SECTION_ICONS[section.icon] || ListBullets;
                  return (
                  <div key={i} className="desc-section">
                    <h3 className="desc-section-title">
                      <SectionIcon size={16} weight="duotone" /> {section.title}
                    </h3>
                    {section.items && (
                      <ul className="desc-section-list">
                        {section.items.map((item: string, j: number) => (
                          <li key={j}>{item}</li>
                        ))}
                      </ul>
                    )}
                    {section.subsections && section.subsections.map((sub: any, k: number) => (
                      <div key={k} className="desc-subsection">
                        <h4 className="desc-subsection-title">{sub.title}</h4>
                        <ul className="desc-section-list">
                          {sub.items.map((item: string, j: number) => (
                            <li key={j}>{item}</li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                  );
                })}
              </div>
            ) : description ? (
              <div className="description-content" dangerouslySetInnerHTML={{ __html: description.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/<[^>]+>/g, '\n').replace(/\n{3,}/g, '\n\n') }} />
            ) : fetchingDetails ? (
              <div className="description-empty">
                <div className="description-empty-icon">
                  <FileText size={32} weight="duotone" />
                </div>
                <p className="description-empty-title">Fetching job description…</p>
                <p className="description-empty-subtitle">
                  Pulling details from the company apply page.
                </p>
              </div>
            ) : (
              <div className="description-empty">
                <div className="description-empty-icon">
                  <FileText size={32} weight="duotone" />
                </div>
                <p className="description-empty-title">No description available</p>
                <p className="description-empty-subtitle">
                  {closed
                    ? "This posting has closed, so its full text is no longer available."
                    : "We couldn't pull this posting's text automatically. The original post has the full details."}
                </p>
                {!closed && (
                  <a
                    href={applyUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-outline-detail"
                    style={{ marginTop: 12 }}
                    onClick={onApplyClick}
                  >
                    <ArrowSquareOut size={16} weight="bold" /> View Original Post
                  </a>
                )}
              </div>
            )}
          </div>

          {job.applicant_count != null && job.applicant_count > 0 && (
            <div className="job-detail-meta-row">
              <Users size={15} weight="duotone" />
              <span>{job.applicant_count}+ applicants</span>
            </div>
          )}
        </div>

        {/* Sidebar: Match Score */}
        <div className="job-detail-sidebar">
          {loading && (
            <div className="match-loading-card">
              <div className="spinner" />
              <span>Scoring your fit...</span>
            </div>
          )}

          {error && <div className="match-error-card">{error}</div>}

          {/* The card's result stays up while the breakdown loads. */}
          {headline && (
            <div className="match-score-card fit-card" data-tour="fit-breakdown">
              {scored && score > 0 && (
                <div className={`match-circle-large${interval ? " fit-range" : ""}`}>
                  <svg viewBox="0 0 100 100" aria-hidden="true">
                    <circle cx="50" cy="50" r="42" fill="none" stroke="#e5e7eb" strokeWidth="6" />
                    <circle
                      cx="50" cy="50" r="42"
                      fill="none" stroke={color} strokeWidth="6"
                      strokeDasharray={
                        interval
                          ? `${Math.max(1, interval.high - interval.low) * 2.64} 264`
                          : `${Math.min(100, score) * 2.64} 264`
                      }
                      strokeDashoffset={interval ? -interval.low * 2.64 : undefined}
                      strokeLinecap="round"
                      transform="rotate(-90 50 50)"
                    />
                  </svg>
                  <span className="match-number-large">
                    {interval ? `${interval.low}–${interval.high}` : score}
                  </span>
                </div>
              )}
              {headline && (
                <p className="fit-headline" style={scored ? { color } : undefined}>{headline}</p>
              )}
              {/* The basis names the point score ("Better fit than 76% ..."), so a displayed
                  interval hides it too. */}
              {scored && !interval && breakdown?.basis && <p className="fit-basis">{breakdown.basis}</p>}
            </div>
          )}

          {breakdown && !loading && (
            <div className="fit-breakdown">
              <FitItems title="Strengths" items={strengthItems} impacts={impacts && impacts.slice(0, strengthItems.length)} />
              <FitItems title="Gaps" items={gapItems} impacts={impacts && impacts.slice(strengthItems.length)} />
              <FitTexts title="To improve" items={breakdown.to_improve ?? []} />
              <FitTexts title="Notes" items={breakdown.notes ?? []} className="fit-notes" />
              {breakdown.outcome === "GATE_FAIL" && (
                // Compliance section 2: the candidate can correct the facts and recompute.
                <a className="fit-profile-link" href="/app/profile">Update your application answers</a>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
