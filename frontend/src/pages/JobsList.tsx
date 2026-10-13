import { useState, useEffect, useCallback } from "react";
import { Logo } from "../components/brand";
import "../jobslist.css";

const API_BASE = "";

interface Job {
  id: number;
  title: string;
  company: string;
  location: string;
  url: string;
  salary_range: string;
  work_type: string;
  role_category: string;
  country: string;
  posted_date: string | null;
  scraped_at: string;
}

interface Stats {
  total: number;
  new: number;
}

const CATEGORIES = [
  "Software Engineering",
  "Data Analysis",
  "Machine Learning and AI",
  "Product Management",
  "Marketing",
  "Engineering and Development",
  "Accounting/Finance",
  "Sales",
  "Design",
  "Human Resources",
  "DevOps/Infrastructure",
  "Cybersecurity",
  "Business Analyst",
  "Customer Support",
  "Legal",
  "Operations",
  "Other",
];

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return "-";
  const date = new Date(dateStr);
  const diff = Date.now() - date.getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 0) return "Just now";
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours > 1 ? "s" : ""} ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "1 day ago";
  if (days < 30) return `${days} days ago`;
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function updatedAgo(dateStr: string | null): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const diff = Date.now() - date.getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "Updated just now";
  if (minutes < 60) return `Updated ${minutes} minutes ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Updated ${hours} hour${hours > 1 ? "s" : ""} ago`;
  return `Updated ${Math.floor(hours / 24)} day${Math.floor(hours / 24) > 1 ? "s" : ""} ago`;
}

function WorkTypeBadge({ type }: { type: string }) {
  const normalized = type?.toLowerCase() || "";
  let label = type || "-";
  let className = "wt-badge";

  if (normalized.includes("remote")) {
    label = "Remote";
    className += " wt-remote";
  } else if (normalized.includes("hybrid")) {
    label = "Hybrid";
    className += " wt-hybrid";
  } else if (normalized.includes("on") || normalized.includes("site") || normalized.includes("office")) {
    label = "On Site";
    className += " wt-onsite";
  }

  return <span className={className}>{label}</span>;
}

export default function JobsList() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [stats, setStats] = useState<Stats>({ total: 0, new: 0 });
  const [country, setCountry] = useState("US");
  const [category, setCategory] = useState("Software Engineering");
  const [workType, setWorkType] = useState("");
  const [search, setSearch] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [companyFilter, setCompanyFilter] = useState("");
  const [locationFilter, setLocationFilter] = useState("");
  const [salaryFilter, setSalaryFilter] = useState("");
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const pageSize = 50;

  const fetchJobs = useCallback(async () => {
    setLoading(true);
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    if (country) params.set("country", country);
    if (category) params.set("role_category", category);
    if (workType) params.set("work_type", workType);
    if (search) params.set("search", search);
    if (locationFilter) params.set("location", locationFilter);

    try {
      const res = await fetch(`${API_BASE}/jobs?${params.toString()}`);
      if (res.ok) {
        const data: Job[] = await res.json();
        setJobs(data);
        setHasMore(data.length === pageSize);
        if (data.length > 0) {
          setLastUpdated(data[0].scraped_at || data[0].posted_date || null);
        }
      }
    } catch {
      // Silently fail
    } finally {
      setLoading(false);
    }
  }, [page, country, category, workType, search, locationFilter]);

  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/jobs/stats`);
      if (res.ok) {
        const data = await res.json();
        setStats({ total: data.total || 0, new: data.new || 0 });
      }
    } catch {
      // Silently fail
    }
  }, []);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  const handleSearch = () => {
    setSearch(searchInput);
    setPage(1);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") handleSearch();
  };

  // Client-side filters for company and salary (API may not support these)
  const filteredJobs = jobs.filter((job) => {
    if (companyFilter && !job.company.toLowerCase().includes(companyFilter.toLowerCase())) return false;
    if (salaryFilter && !job.salary_range?.toLowerCase().includes(salaryFilter.toLowerCase())) return false;
    return true;
  });

  return (
    <div className="jl-page">
      {/* Top Navigation */}
      <nav className="jl-navbar">
        <div className="jl-nav-inner">
          <div className="jl-nav-left">
            <a href="/" className="jl-logo">
              <Logo variant="mark" theme="light" size={32} className="jl-logo-img" />
              <span className="jl-logo-text">Tailrd</span>
            </a>
          </div>
          <div className="jl-nav-right">
            <a href="/" className="jl-nav-link">Home</a>
            <a href="/list" className="jl-nav-link jl-nav-link-active">2026 New Grad Jobs</a>
            <a href="/list?category=Internships" className="jl-nav-link">Internships</a>
          </div>
        </div>
      </nav>

      {/* Hero Section */}
      <section className="jl-hero">
        <h1 className="jl-hero-title">
          The Ultimate 2026 U.S. &amp; Canada New Grad &amp; Internship List
        </h1>
        <p className="jl-hero-subtitle">
          Get hourly updates from 200+ company career sites with direct apply links.
        </p>
        <div className="jl-hero-stats">
          <div className="jl-stat">
            <span className="jl-stat-number">{stats.new.toLocaleString()}</span>
            <span className="jl-stat-label">New Openings Today</span>
          </div>
          <div className="jl-stat-divider" />
          <div className="jl-stat">
            <span className="jl-stat-number">{stats.total.toLocaleString()}</span>
            <span className="jl-stat-label">Total Openings</span>
          </div>
        </div>
        {lastUpdated && (
          <p className="jl-hero-updated">{updatedAgo(lastUpdated)}</p>
        )}
      </section>

      {/* Country Toggle */}
      <div className="jl-country-toggle">
        <button
          className={`jl-country-btn ${country === "US" ? "active" : ""}`}
          onClick={() => { setCountry("US"); setPage(1); }}
        >
          <img src="https://flagcdn.com/w40/us.png" alt="US" className="jl-flag" /> United States
        </button>
        <button
          className={`jl-country-btn ${country === "CA" ? "active" : ""}`}
          onClick={() => { setCountry("CA"); setPage(1); }}
        >
          <img src="https://flagcdn.com/w40/ca.png" alt="CA" className="jl-flag" /> Canada
        </button>
      </div>

      {/* City Quick Filters */}
      <div className="jl-categories jl-cities">
        <div className="jl-categories-scroll">
          {(country === "CA"
            ? ["Toronto", "Vancouver", "Montreal", "Ottawa", "Calgary", "Waterloo"]
            : ["San Francisco", "New York", "Seattle", "Austin", "Boston", "Chicago", "Denver", "Remote"]
          ).map((city) => (
            <button
              key={city}
              className={`jl-cat-pill ${locationFilter === city ? "active" : ""}`}
              onClick={() => { setLocationFilter(locationFilter === city ? "" : city); setPage(1); }}
            >
              {city}
            </button>
          ))}
        </div>
      </div>

      {/* Category Pills */}
      <div className="jl-categories">
        <div className="jl-categories-scroll">
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              className={`jl-cat-pill ${category === cat ? "active" : ""}`}
              onClick={() => { setCategory(category === cat ? "" : cat); setPage(1); }}
            >
              {cat}
            </button>
          ))}
        </div>
      </div>

      {/* Filter Row */}
      <div className="jl-filters">
        <div className="jl-filter-item">
          <input
            type="text"
            placeholder="Search title..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={handleKeyDown}
            className="jl-filter-input"
          />
          <button className="jl-filter-search-btn" onClick={handleSearch}>
            Search
          </button>
        </div>
        <div className="jl-filter-item">
          <input
            type="text"
            placeholder="Company"
            value={companyFilter}
            onChange={(e) => setCompanyFilter(e.target.value)}
            className="jl-filter-input"
          />
        </div>
        <div className="jl-filter-item">
          <select
            value={workType}
            onChange={(e) => { setWorkType(e.target.value); setPage(1); }}
            className="jl-filter-select"
          >
            <option value="">Work Model</option>
            <option value="remote">Remote</option>
            <option value="hybrid">Hybrid</option>
            <option value="onsite">On Site</option>
          </select>
        </div>
        <div className="jl-filter-item">
          <input
            type="text"
            placeholder="Location"
            value={locationFilter}
            onChange={(e) => setLocationFilter(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { setPage(1); fetchJobs(); } }}
            className="jl-filter-input"
          />
        </div>
        <div className="jl-filter-item">
          <input
            type="text"
            placeholder="Salary"
            value={salaryFilter}
            onChange={(e) => setSalaryFilter(e.target.value)}
            className="jl-filter-input"
          />
        </div>
      </div>

      {/* Job Table */}
      <div className="jl-table-wrapper">
        {loading ? (
          <div className="jl-loading">Loading jobs...</div>
        ) : filteredJobs.length === 0 ? (
          <div className="jl-empty">No jobs found. Try adjusting your filters.</div>
        ) : (
          <table className="jl-table">
            <thead>
              <tr>
                <th className="jl-th-num">#</th>
                <th className="jl-th-title">Position Title</th>
                <th className="jl-th-date">Date</th>
                <th className="jl-th-apply">Apply</th>
                <th className="jl-th-workmodel">Work Model</th>
                <th className="jl-th-location">Location</th>
                <th className="jl-th-company">Company</th>
                <th className="jl-th-salary">Salary</th>
              </tr>
            </thead>
            <tbody>
              {filteredJobs.map((job, idx) => (
                <tr key={job.id} className={idx % 2 === 0 ? "jl-row-even" : "jl-row-odd"}>
                  <td className="jl-td-num">{(page - 1) * pageSize + idx + 1}</td>
                  <td className="jl-td-title">{job.title.replace(/\*\*/g, "")}</td>
                  <td className="jl-td-date">{timeAgo(job.posted_date || job.scraped_at)}</td>
                  <td className="jl-td-apply">
                    <a
                      href={job.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="jl-apply-btn"
                    >
                      Apply
                    </a>
                  </td>
                  <td className="jl-td-workmodel">
                    <WorkTypeBadge type={job.work_type} />
                  </td>
                  <td className="jl-td-location">{job.location || "-"}</td>
                  <td className="jl-td-company">{job.company.replace(/\*\*/g, "")}</td>
                  <td className="jl-td-salary">{job.salary_range || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      {!loading && filteredJobs.length > 0 && (
        <div className="jl-pagination">
          <button
            className="jl-page-btn"
            disabled={page === 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            ← Previous
          </button>
          <span className="jl-page-info">Page {page}</span>
          <button
            className="jl-page-btn"
            disabled={!hasMore}
            onClick={() => setPage((p) => p + 1)}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  );
}
