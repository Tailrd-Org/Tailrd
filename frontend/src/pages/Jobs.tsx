import { useState, useEffect, useMemo, useRef } from "react";
import JobFilterBar, { JobFilters, normalizeExperienceLevels } from "../components/JobFilterBar";
import JobDetailView from "../components/JobDetailView";
import CustomResumeModal, { type AIJob } from "../components/CustomResumeModal";
import CoverLetterModal from "../components/CoverLetterModal";
import JobsSidebar, { type SidebarData } from "../components/JobsSidebar";
import { skillLabel } from "../lib/skillLabel";
import api from "../auth/api";
import { useApplyTracking } from "../context/ApplyTracking";
import CompanyLogo from "../components/CompanyLogo";
import { cleanCompanyName } from "../lib/companyLogo";
import { displayLocation } from "../lib/jobLocation";
import { handleApplyLinkClick, isListingClosed, postedAgo } from "../lib/jobListing";
import {
  MagnifyingGlass,
  Sliders,
  Clock,
  BookmarkSimple,
  MapPin,
  Briefcase,
  House,
  GraduationCap,
  Money,
  Users,
  ThumbsDown,
  ShareNetwork,
  PencilSimple,
  Envelope,
  CaretLeft,
  CaretRight,
  Prohibit,
  Sparkle,
  X,
} from "@phosphor-icons/react";



interface Job {
  id: number;
  title: string;
  company: string;
  location: string;
  url: string;
  description: string;
  match_score: number;
  match_summary: string;
  match_label: string;
  salary_range: string;
  company_size: string;
  status: string;
  easy_apply: boolean;
  ats_type: string;
  scraped_at: string;
  source_platform: string;
  saved: boolean;
  experience_score: number;
  skill_score: number;
  industry_score: number;
  applicant_count: number | null;
  company_logo: string;
  company_domain?: string;
  company_url?: string;
  work_type: string;
  role_category: string;
  country: string;
  experience_level: string;
  posted_date: string | null;
  locations_json?: { city?: string; region?: string; region_name?: string; country?: string }[] | null;
  // active | stale | removed | expired. The feed only lists active and stale
  // rows; removed/expired ones arrive via the Liked tab, deep links, or a live check.
  listing_status?: string | null;
  // Canonical skill tags extracted at ingest (lowercase, see lib/skillLabel).
  skills?: string[] | null;
}

interface Stats {
  total: number;
  applied: number;
  new: number;
  avg_match_score: number;
  saved_count: number;
}

interface Filters {
  source: string;
  min_match_score: number;
  experience_level: string;
}

// Legacy GitHub-list rows store "**Company**"; clean once at the API boundary
// so the card, the detail panel and the AI modals all show the plain name.
function normalizeJob(job: Job): Job {
  return { ...job, company: cleanCompanyName(job.company) };
}

const FILTER_STORAGE_KEY = "job-aggregator-filters";
const LAST_VISIT_KEY = "tailrd.jobs.lastVisit";

// A filter the right rail applies; shown as a removable chip above the feed.
type QuickFilter = { kind: "strong" } | { kind: "new"; since: string } | null;

/** When the user last opened the Jobs page, read once per page load. */
function readLastVisit(): string | null {
  try {
    return localStorage.getItem(LAST_VISIT_KEY);
  } catch {
    return null;
  }
}

export default function Jobs() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [stats, setStats] = useState<Stats>({ total: 0, applied: 0, new: 0, avg_match_score: 0, saved_count: 0 });
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("All");
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(true);
  const pageSize = 50;
  const [search, setSearch] = useState("");
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [rewriteJob, setRewriteJob] = useState<AIJob | null>(null);
  const [coverJob, setCoverJob] = useState<AIJob | null>(null);
  const [filtersVisible, setFiltersVisible] = useState(true);
  const { registerApplyClick } = useApplyTracking();
  const [sidebar, setSidebar] = useState<SidebarData | null>(null);
  const [quickFilter, setQuickFilter] = useState<QuickFilter>(null);
  const [lastVisit] = useState(readLastVisit);

  const jobsListRef = useRef<HTMLDivElement>(null);
  const prevSelectedJobRef = useRef<Job | null>(null);
  // A selected job that is expected to be missing from the current page (a
  // deep link, or a listing the live check just closed): exempt from auto-close.
  const pinnedJobIdRef = useRef<number | null>(null);
  // Current tab and selected job, for live-check answers that arrive after
  // the render that asked for them.
  const activeTabRef = useRef(activeTab);
  activeTabRef.current = activeTab;
  const selectedJobIdRef = useRef<number | null>(null);
  selectedJobIdRef.current = selectedJob?.id ?? null;

  const [filters] = useState<Filters>({
    source: "",
    min_match_score: 0,
    experience_level: "",
  });

  const [aggFilters, setAggFilters] = useState<JobFilters>(() => {
    try {
      const saved = localStorage.getItem(FILTER_STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        return {
          country: parsed.country || "",
          location: Array.isArray(parsed.location)
            ? parsed.location
            : typeof parsed.location === "string" && parsed.location
              ? [parsed.location]
              : [],
          work_type: Array.isArray(parsed.work_type) ? parsed.work_type : [],
          role_category: Array.isArray(parsed.role_category) ? parsed.role_category : [],
          experience_level: normalizeExperienceLevels(parsed.experience_level),
          date_posted: parsed.date_posted || "",
        };
      }
    } catch {
      // Ignore parse errors, use defaults
    }
    return { country: "", location: [], work_type: [], role_category: [], experience_level: [], date_posted: "" };
  });

  useEffect(() => {
    try {
      localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify(aggFilters));
    } catch {
      // Ignore storage errors (e.g., quota exceeded)
    }
  }, [aggFilters]);

  // Close detail panel on Escape key press
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && selectedJob) {
        setSelectedJob(null);
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [selectedJob]);

  // Restore focus to job list when detail panel closes
  useEffect(() => {
    if (prevSelectedJobRef.current && !selectedJob) {
      jobsListRef.current?.focus();
    }
    prevSelectedJobRef.current = selectedJob;
  }, [selectedJob]);

  // Open a specific job when arriving from a match-alert email (?job=<id>).
  const deepLinkHandledRef = useRef(false);
  useEffect(() => {
    if (deepLinkHandledRef.current) return;
    const params = new URLSearchParams(window.location.search);
    const jobIdParam = params.get("job");
    if (!jobIdParam) return;
    deepLinkHandledRef.current = true;
    const jobId = Number(jobIdParam);
    (async () => {
      try {
        if (Number.isFinite(jobId)) {
          const res = await api.get(`/jobs/${jobId}`);
          // The linked job is rarely on the first page; keep it open anyway.
          // A hidden duplicate resolves to its visible twin, so pin the id
          // the API actually returned.
          pinnedJobIdRef.current = res.data?.id ?? jobId;
          setSelectedJob(normalizeJob(res.data));
        }
      } catch {
        // Job not found or not accessible, fall back to the list.
      } finally {
        // Strip the param so refreshes don't re-open the panel.
        params.delete("job");
        const qs = params.toString();
        window.history.replaceState(
          {},
          "",
          window.location.pathname + (qs ? `?${qs}` : ""),
        );
      }
    })();
  }, []);

  useEffect(() => {
    fetchJobs();
    fetchStats();
  }, [page, activeTab, filters, aggFilters, search, quickFilter]);

  // Read the previous visit before stamping this one, so "new since your last
  // visit" counts from then, not from a moment ago.
  useEffect(() => {
    try {
      localStorage.setItem(LAST_VISIT_KEY, new Date().toISOString());
    } catch {
      // Blocked storage: the rail falls back to "the last 24 hours".
    }
    fetchSidebar();
  }, []);

  async function fetchSidebar() {
    try {
      const res = await api.get("/jobs/sidebar", {
        params: lastVisit ? { since: lastVisit } : undefined,
      });
      // Guard the shape: a proxy error page or an old backend must not
      // reach the rail as data.
      if (res.data && typeof res.data === "object" && "feed" in res.data) setSidebar(res.data);
    } catch {
      // The rail is extra; the feed works without it.
    }
  }

  const resumeSkills = useMemo(() => new Set(sidebar?.resume_skills ?? []), [sidebar]);

  async function fetchJobs() {
    setLoading(true);
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", String(pageSize));

    if (activeTab === "Liked") params.set("saved", "1");
    if (activeTab === "All") params.set("sort", "match");
    if (quickFilter?.kind === "strong") params.set("strong", "1");
    if (quickFilter?.kind === "new") params.set("since", quickFilter.since);
    if (filters.source) params.set("source", filters.source);
    if (filters.min_match_score > 0) params.set("min_score", String(filters.min_match_score));
    if (search.trim()) params.set("search", search.trim());

    if (aggFilters.country) params.set("country", aggFilters.country);
    if (aggFilters.location.length > 0) {
      // ";" join: a single tag may itself contain a comma ("Ottawa, ON").
      const locationParam = aggFilters.location.map(c => c.trim()).filter(c => c.length > 0).join(";");
      if (locationParam) params.set("location", locationParam);
    }
    if (aggFilters.work_type.length > 0) params.set("work_type", aggFilters.work_type.join(","));
    if (aggFilters.role_category.length > 0) params.set("role_category", aggFilters.role_category.join(","));
    if (aggFilters.experience_level.length > 0) params.set("experience_level", aggFilters.experience_level.join(","));
    if (aggFilters.date_posted) params.set("date_posted", aggFilters.date_posted);

    try {
      const res = await api.get("/jobs", { params });
      setJobs((res.data as Job[]).map(normalizeJob));
      setHasMore(res.data.length === pageSize);
    } catch {
      // Silently fail
    } finally {
      setLoading(false);
    }
  }

  async function fetchStats() {
    try {
      const res = await api.get("/jobs/stats");
      setStats(res.data);
    } catch {
      // Silently fail
    }
  }

  // Auto-dismiss the save error after a few seconds.
  useEffect(() => {
    if (!saveError) return;
    const t = setTimeout(() => setSaveError(null), 4000);
    return () => clearTimeout(t);
  }, [saveError]);

  async function toggleSave(job: Job) {
    const next = !job.saved;
    const endpoint = next ? `/jobs/${job.id}/save` : `/jobs/${job.id}/unsave`;

    // Optimistic: flip the bookmark immediately so it feels responsive.
    setJobs((prev) =>
      prev.map((j) => (j.id === job.id ? { ...j, saved: next } : j))
    );

    try {
      await api.post(endpoint);
      fetchStats();
      fetchSidebar();
    } catch (err) {
      // Revert the optimistic change and tell the user it didn't stick.
      setJobs((prev) =>
        prev.map((j) => (j.id === job.id ? { ...j, saved: !next } : j))
      );
      const status = (err as { response?: { status?: number } })?.response?.status;
      setSaveError(
        status === 403
          ? "Verify your email to save jobs."
          : "Couldn't save the job. Please try again."
      );
    }
  }

  // A live check (the detail panel's, or an Apply click's) found the
  // listing's status changed. Verdicts can land after the user has moved on to
  // another job or tab, so this reads the current ones through refs, never
  // from the render that started the check.
  function handleListingStatusChange(
    jobId: number,
    listingStatus: string,
    options: { keepInFeed?: boolean } = {},
  ) {
    const closed = isListingClosed(listingStatus);
    // The panel stays open on the closed state even as the row leaves the
    // feed, but only the job on screen may take the pin.
    if (closed && selectedJobIdRef.current === jobId) pinnedJobIdRef.current = jobId;
    setSelectedJob((s) => (s && s.id === jobId ? { ...s, listing_status: listingStatus } : s));
    setJobs((prev) =>
      // A dead listing drops out of the feed; the Liked tab keeps saved jobs
      // and shows them as closed instead, and so does a card whose own Apply
      // click just found it closed.
      closed && activeTabRef.current !== "Liked" && !options.keepInFeed
        ? prev.filter((j) => j.id !== jobId)
        : prev.map((j) => (j.id === jobId ? { ...j, listing_status: listingStatus } : j))
    );
    // An open AI modal for this job must stop offering its dead link.
    const markClosed = (job: AIJob | null) =>
      job && job.id === jobId ? { ...job, closed } : job;
    setRewriteJob(markClosed);
    setCoverJob(markClosed);
  }

  // Card Apply: a short live check first, so a dead listing never opens.
  function onCardApplyClick(event: Parameters<typeof handleApplyLinkClick>[0], job: Job) {
    handleApplyLinkClick(event, job.id, job.url, {
      onOpened: () => registerApplyClick({ id: job.id, title: job.title, company: job.company, url: job.url }),
      // Keep the card where the user clicked it, now marked closed.
      onClosed: (status) => handleListingStatusChange(job.id, status, { keepInFeed: true }),
    });
  }

  function aiJob(job: Job): AIJob {
    return {
      id: job.id,
      title: job.title,
      company: job.company,
      url: job.url,
      closed: isListingClosed(job.listing_status),
    };
  }

  // Open a job the rail points at (a saved job closing soon), which is often
  // not on the current page, so pin it like a deep link.
  async function openJobById(jobId: number) {
    try {
      const res = await api.get(`/jobs/${jobId}`);
      pinnedJobIdRef.current = res.data?.id ?? jobId;
      setSelectedJob(normalizeJob(res.data));
    } catch {
      // Gone since the rail loaded; nothing to open.
    }
  }

  function applyQuickFilter(next: QuickFilter) {
    setQuickFilter(next);
    setActiveTab("All");
    setPage(1);
    jobsListRef.current?.scrollTo?.({ top: 0 });
  }

  async function setMatchAlerts(enabled: boolean) {
    await api.put("/settings", { match_alerts_enabled: enabled });
    setSidebar((s) => (s ? { ...s, alerts_enabled: enabled } : s));
  }

  const TABS = [
    { label: "All", count: null },
    { label: "Liked", count: stats.saved_count },
  ];

  const filteredJobs = jobs.filter((j) => {
    if (activeTab === "Liked") return j.saved;
    return true;
  });

  // Auto-close detail panel when selected job is filtered out (a pinned job is
  // not expected on this page, so it stays open)
  useEffect(() => {
    if (
      selectedJob &&
      selectedJob.id !== pinnedJobIdRef.current &&
      !filteredJobs.some((j) => j.id === selectedJob.id)
    ) {
      setSelectedJob(null);
    }
  }, [filteredJobs, selectedJob]);

  return (
    <div className="jobs-page">
      {saveError && (
        <div className="toast toast-error" role="alert" style={{ margin: "0 0 12px" }}>
          {saveError}
        </div>
      )}
      {/* Header Bar: title + tabs share one line, search sits on the right */}
      <header className="jobs-header">
        <h1>Jobs</h1>
        <div className="jobs-tabs">
          {TABS.map((tab) => (
            <button
              key={tab.label}
              className={`tab-btn ${activeTab === tab.label ? "active" : ""}`}
              onClick={() => { setActiveTab(tab.label); setPage(1); }}
            >
              {tab.label}
              {tab.count !== null && tab.count > 0 && (
                <span className="tab-count">{tab.count}</span>
              )}
            </button>
          ))}
        </div>
        <div className="header-right">
          <div className="search-wrapper">
            <MagnifyingGlass size={16} weight="bold" />
            <input
              type="text"
              placeholder="Search by title or company"
              className="search-input"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>
      </header>

      {/* Filter Bar */}
      <div className="jobs-toolbar">
        <div className="jobs-toolbar-top">
          <button
            className="filter-toggle-btn"
            onClick={() => setFiltersVisible(!filtersVisible)}
            aria-label={filtersVisible ? "Hide filters" : "Show filters"}
          >
            <Sliders size={15} weight="bold" />
            {filtersVisible ? "Hide Filters" : "Show Filters"}
          </button>
          {quickFilter && (
            <span className="jobs-quick-filter">
              {quickFilter.kind === "strong" ? "Strong matches only" : "New since your last visit"}
              <button type="button" onClick={() => applyQuickFilter(null)} aria-label="Clear quick filter">
                <X size={12} weight="bold" />
              </button>
            </span>
          )}
        </div>
        {filtersVisible && (
          <div data-tour="job-filters">
            <JobFilterBar
              filters={aggFilters}
              onChange={(newFilters) => { setAggFilters(newFilters); setPage(1); }}
            />
          </div>
        )}
      </div>

      {/* Content Area: Job Feed + Detail Panel */}
      <div className={`jobs-content-area${selectedJob ? " has-detail" : " has-rail"}`}>
        {/* Job Feed */}
        <div className="jobs-feed" data-tour="jobs-list" ref={jobsListRef} tabIndex={-1}>
          {loading && <p className="loading-text">Loading jobs...</p>}
          {!loading && filteredJobs.length === 0 && (
            <p className="empty-text">No jobs found. Start the scraper or adjust your filters.</p>
          )}

          {filteredJobs.map((job, jobIndex) => (
            <div
              key={job.id}
              data-tour={jobIndex === 0 ? "job-card" : undefined}
              className={`job-card${selectedJob?.id === job.id ? " selected" : ""}`}
              onClick={() => {
                pinnedJobIdRef.current = null;
                setSelectedJob(job);
              }}
              style={{ cursor: "pointer" }}
            >
              <div className="job-card-body">
                {/* Header: Logo + Info + Bookmark */}
                <div className="job-card-header">
                  <CompanyLogo
                    company={job.company}
                    company_logo={job.company_logo}
                    company_domain={job.company_domain}
                    company_url={job.company_url}
                    size={44}
                  />
                  <div className="job-card-info">
                    <div className="job-card-badges">
                      <span className="badge-time">
                        <Clock size={13} weight="duotone" /> {postedAgo(job.posted_date || job.scraped_at)}
                      </span>
                      {isListingClosed(job.listing_status) && (
                        <span className="listing-closed-badge">
                          <Prohibit size={12} weight="bold" /> No longer accepting applications
                        </span>
                      )}
                    </div>
                    <h2 className="job-title">{job.title}</h2>
                    <p className="job-company">
                      {job.company}
                      <span className="company-meta"> · {job.company_size || "Technology"}</span>
                    </p>
                  </div>
                  <button
                    className={`btn-bookmark ${job.saved ? "saved" : ""}`}
                    onClick={(e) => { e.stopPropagation(); toggleSave(job); }}
                    aria-label={job.saved ? "Unsave job" : "Save job"}
                  >
                    <BookmarkSimple size={20} weight={job.saved ? "fill" : "regular"} />
                  </button>
                </div>

                {/* Details Grid */}
                <div className="job-details-grid">
                  <div className="job-detail-item" title={job.location || undefined}>
                    <MapPin size={15} weight="duotone" />
                    <span>{displayLocation(job) || "Remote"}</span>
                  </div>
                  <div className="job-detail-item">
                    <Briefcase size={15} weight="duotone" />
                    <span>Full-time</span>
                  </div>
                  <div className="job-detail-item">
                    <House size={15} weight="duotone" />
                    <span>{job.work_type === "remote" ? "Remote" : job.work_type === "hybrid" ? "Hybrid" : "On Site"}</span>
                  </div>
                  <div className="job-detail-item">
                    <GraduationCap size={15} weight="duotone" />
                    <span>{job.experience_level === "internship" ? "Internship" : "Entry, New Grad"}</span>
                  </div>
                  {job.salary_range && !job.salary_range.startsWith("{") && (
                    <div className="job-detail-item">
                      <Money size={15} weight="duotone" />
                      <span className="salary">{job.salary_range}</span>
                    </div>
                  )}
                </div>

                {/* Why it matches: résumé skills this posting also asks for */}
                {(() => {
                  const shared = (job.skills ?? []).filter((t) => resumeSkills.has(t));
                  if (shared.length === 0) return null;
                  return (
                    <div className="job-why-match">
                      <Sparkle size={13} weight="fill" />
                      <span>Matches your résumé:</span>
                      {shared.slice(0, 4).map((t) => (
                        <span key={t} className="job-why-chip">{skillLabel(t)}</span>
                      ))}
                      {shared.length > 4 && <span>+{shared.length - 4} more</span>}
                    </div>
                  );
                })()}

                {/* Footer */}
                <div className="job-card-footer" onClick={(e) => e.stopPropagation()}>
                  <span className="applicant-text">
                    {job.applicant_count
                      ? <><Users size={14} weight="duotone" /> {job.applicant_count}+ applicants</>
                      : ""}
                  </span>
                  <div className="job-actions">
                    <button className="btn-icon" title="Not interested">
                      <ThumbsDown size={15} weight="bold" />
                    </button>
                    <button className="btn-icon" title="Share">
                      <ShareNetwork size={15} weight="bold" />
                    </button>
                    <button
                      className="btn-ai"
                      onClick={(e) => { e.stopPropagation(); setRewriteJob(aiJob(job)); }}
                    >
                      <PencilSimple size={15} weight="fill" /> Suggest Edits
                    </button>
                    <button
                      className="btn-ai"
                      onClick={(e) => { e.stopPropagation(); setCoverJob(aiJob(job)); }}
                    >
                      <Envelope size={15} weight="fill" /> Cover Letter Ideas
                    </button>
                    {isListingClosed(job.listing_status) ? (
                      // Closed listing: never navigate to its dead link.
                      <button type="button" className="btn-apply" disabled>
                        APPLY WITH TAILRD
                      </button>
                    ) : (
                      <a
                        href={job.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="btn-apply"
                        onClick={(e) => onCardApplyClick(e, job)}
                      >
                        APPLY WITH TAILRD
                      </a>
                    )}
                  </div>
                </div>
              </div>

              {/* Match Score Badge */}
              {job.match_score > 0 && (
                <div className="match-score-badge">
                  <div className="match-circle-sm">
                    <svg viewBox="0 0 100 100">
                      <circle cx="50" cy="50" r="40" fill="none" stroke="#e3e8ee" strokeWidth="8" />
                      <circle cx="50" cy="50" r="40" fill="none" stroke="#533afd" strokeWidth="8"
                        strokeDasharray={`${job.match_score * 2.51} 251`}
                        strokeLinecap="round"
                        style={{ transform: "rotate(-90deg)", transformOrigin: "center" }}
                      />
                    </svg>
                    <span className="match-pct">{job.match_score}<small>%</small></span>
                  </div>
                  <span className="match-label-sm">
                    {job.match_score >= 80 ? "STRONG" : job.match_score >= 60 ? "GOOD" : "FAIR"} MATCH
                  </span>
                </div>
              )}
            </div>
          ))}

          {/* Pagination */}
          {!loading && filteredJobs.length > 0 && (
            <div className="pagination">
              <button
                className="btn-outline"
                disabled={page === 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                <CaretLeft size={14} weight="bold" /> Previous
              </button>
              <span className="page-indicator">Page {page}</span>
              <button
                className="btn-outline"
                disabled={!hasMore}
                onClick={() => setPage((p) => p + 1)}
              >
                Next <CaretRight size={14} weight="bold" />
              </button>
            </div>
          )}
        </div>

        {!selectedJob && (
          <JobsSidebar
            data={sidebar}
            onShowStrong={() => applyQuickFilter({ kind: "strong" })}
            onShowNew={(since) => applyQuickFilter({ kind: "new", since })}
            onShowRemote={() => {
              applyQuickFilter(null);
              setAggFilters((f) => ({ ...f, work_type: ["remote"] }));
              setFiltersVisible(true);
            }}
            onCompany={(company) => {
              applyQuickFilter(null);
              setSearch(company);
            }}
            onOpenJob={openJobById}
            onAlertsChange={setMatchAlerts}
          />
        )}

        {/* Inline Job Detail Panel */}
        {selectedJob && (
          <div className="job-detail-inline">
            <JobDetailView
              job={selectedJob}
              onClose={() => setSelectedJob(null)}
              onListingStatusChange={handleListingStatusChange}
            />
          </div>
        )}
      </div>

      {rewriteJob && <CustomResumeModal job={rewriteJob} onClose={() => setRewriteJob(null)} />}
      {coverJob && <CoverLetterModal job={coverJob} onClose={() => setCoverJob(null)} />}
    </div>
  );
}
