/**
 * Deterministic API fixtures for the responsive audit.
 *
 * Content is deliberately "realistic worst case": long company names, long job
 * titles, long unbroken URLs and emails, many skill tags. Overflow bugs hide
 * behind short lorem-ipsum data, so the fixtures use the kind of strings the
 * app actually sees in production.
 */

const LONG_TITLE =
  "Senior Software Development Engineer II, Distributed Systems & Platform Infrastructure";
const LONG_COMPANY = "Hoffman-Rutherford Global Technology Solutions International";
const LONG_EMAIL = "wissam.elmasry.developer.candidate@averylongcorporatedomainname.example.com";
const LONG_URL =
  "https://boards.greenhouse.io/averylongcompanyslug/jobs/1234567890?gh_jid=1234567890&utm_source=tailrd&utm_campaign=job_alerts_daily_digest";

const user = {
  id: 42,
  email: LONG_EMAIL,
  first_name: "Wissam",
  last_name: "Elmasry",
  email_verified: true,
  has_completed_onboarding: true,
  has_completed_setup: true,
  created_at: "2026-01-04T12:00:00Z",
};

const FIT_OUTCOMES = [
  { match_score: 0, match_band: "Limited match", match_outcome: "SCORED_LOW_CONFIDENCE" },
  { match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" },
  { match_score: 0, match_band: null, match_outcome: "GATE_FAIL" },
  { match_score: 88, match_band: "Strong match", match_outcome: "SCORED" },
];

function job(i, overrides = {}) {
  return {
    id: i,
    title: i === 1 ? LONG_TITLE : `Software Engineer ${i}`,
    company: i === 1 ? LONG_COMPANY : `Acme Corp ${i}`,
    location: i === 1 ? "Greater Toronto Area, Ontario, Canada (Hybrid, 3 days onsite)" : "Toronto, ON",
    url: LONG_URL,
    description:
      "About the role\n\nWe are looking for a talented engineer to join our platform team. " +
      "Responsibilities include designing and shipping distributed services, mentoring, and " +
      "partnering with product.\n\nRequirements\n- 3+ years of experience\n- Strong Python/TypeScript\n" +
      "- Experience with Kubernetes, Terraform, PostgreSQL\n\nSupercalifragilisticexpialidocious" +
      "AntidisestablishmentarianismPneumonoultramicroscopicsilicovolcanoconiosis\n\n" +
      LONG_URL,
    // The fit engine's card fields (Task 9.4): every outcome appears, so the
    // longest words-only badges ("We couldn't score this", "Not eligible" + "See why")
    // are measured too.
    ...FIT_OUTCOMES[i % FIT_OUTCOMES.length],
    match_summary:
      "Strong match on backend systems, Python and cloud infrastructure; light on Kubernetes.",
    match_label: "Strong Match",
    salary_range: "$120,000 - $165,000 CAD + equity + annual performance bonus",
    company_size: "1,001-5,000 employees",
    status: i % 3 === 0 ? "applied" : "new",
    easy_apply: i % 2 === 0,
    ats_type: "greenhouse",
    scraped_at: "2026-07-09T15:04:00Z",
    source_platform: "linkedin",
    saved: i % 4 === 0,
    experience_score: 88,
    skill_score: 91,
    industry_score: 74,
    applicant_count: 137,
    company_logo: "",
    company_domain: "example.com",
    company_url: "https://example.com",
    work_type: "Hybrid",
    role_category: "Software Engineering",
    country: "Canada",
    experience_level: "Mid-Senior level",
    posted_date: "2026-07-08T00:00:00Z",
    ...overrides,
  };
}

const jobs = Array.from({ length: 12 }, (_, i) => job(i + 1));

/* ---- POST /ai/match-breakdown/<id>: the fit engine's result for the detail view.
   Worst case: a displayed interval in the ring, long strength and gap labels and notes. ---- */
const matchBreakdown = {
  overall_score: 74, experience_score: 70, skill_score: 81, industry_score: 40,
  match_label: "Good match", strengths: [], weaknesses: [],
  outcome: "SCORED",
  band: "Good match",
  display_score: 74,
  headline: "Fit score 68–79 · Good match",
  basis: "Better fit than 74% of candidates scored for similar roles",
  strength_items: [
    { label: "4 of 5 required skills", impact: "+0.39", note: "Python, PostgreSQL, Kubernetes and Terraform (through your AWS CloudFormation experience)", code: "REQUIRED_SKILLS" },
    { label: "3.5 yrs relevant vs 3 required", impact: "+0.21", note: "", code: "EXPERIENCE" },
  ],
  gaps: [
    { label: "Missing: Apache Flink stream processing (required)", impact: "-0.18", note: "Partly covered by your Kafka Streams and Spark Structured Streaming work", code: "GAP" },
    { label: "This role is one level above your current one", impact: "-0.04", note: "", code: "SENIORITY_GAP" },
  ],
  to_improve: ["Add a project that uses Apache Flink, or describe the stream processing you did with Kafka Streams"],
  notes: ["Docker is only listed in your skills section; showing it in a project or role would count for more."],
  interval: { low: 68, high: 79, display: true },
};

const stats = { total: 1284, applied: 37, new: 412, avg_match_score: 78, saved_count: 19 };

const SKILLS = [
  "Python", "TypeScript", "React", "FastAPI", "PostgreSQL", "Kubernetes", "Terraform",
  "AWS", "Docker", "GraphQL", "Redis", "Kafka", "Distributed Systems Architecture",
  "CI/CD", "Playwright", "System Design", "Machine Learning Operations (MLOps)",
];

const profile = {
  name: "Wissam Elmasry",
  email: LONG_EMAIL,
  phone: "+1 (416) 555-0142",
  location: "Toronto, Ontario, Canada",
  linkedin_url: "https://www.linkedin.com/in/wissam-elmasry-software-engineer-toronto",
  github_url: "https://github.com/wissamelmasry-averylongusernamehere",
  other_link: "https://portfolio.wissamelmasry.example.com/case-studies/platform",
  summary:
    "Backend-leaning full-stack engineer with 5 years building distributed platforms. " +
    "Shipped a job-application autofill engine covering 69 ATS providers; cut median " +
    "application time from 11 minutes to 40 seconds.",
  summary_title: "Professional Summary",
  skills: SKILLS,
  experience: [
    {
      company: LONG_COMPANY,
      title: LONG_TITLE,
      location: "Toronto, ON, Canada",
      start_date: "Jan 2024",
      end_date: "Present",
      bullets: [
        "Architected and shipped a multi-tenant autofill engine spanning 69 applicant tracking systems, raising successful-fill rate from 61% to 94% across 40,000 monthly applications.",
        "Led migration from a monolith to 12 event-driven services on Kubernetes, cutting p99 latency 340ms → 88ms.",
        "Mentored 4 engineers; introduced trunk-based development and a 9-minute CI pipeline.",
      ],
    },
    {
      company: "Northbound Analytics",
      title: "Software Engineer",
      location: "Remote",
      start_date: "Jun 2021",
      end_date: "Dec 2023",
      bullets: [
        "Built the ingestion pipeline processing 2.1B events/day with Kafka and Flink.",
        "Reduced AWS spend 38% by rightsizing and introducing spot fleets.",
      ],
    },
  ],
  education: [
    {
      school: "University of Toronto, Faculty of Applied Science & Engineering",
      degree: "Bachelor of Applied Science, Computer Engineering (Co-op)",
      location: "Toronto, ON",
      start_date: "Sep 2017",
      end_date: "Apr 2021",
      gpa: "3.87 / 4.00",
      achievements: ["Dean's List (all terms)", "NSERC Undergraduate Student Research Award"],
      coursework: ["Distributed Systems", "Compilers", "Operating Systems", "Machine Learning"],
    },
  ],
  projects: [
    {
      name: "Tailrd, AI job-application copilot",
      link: "https://github.com/wissam/tailrd-a-very-long-repository-name-for-testing",
      organization: "Personal",
      location: "",
      start_date: "2025",
      end_date: "Present",
      bullets: [
        "Chrome extension + FastAPI backend that tailors resumes and autofills applications.",
      ],
    },
  ],
  technologies: {
    Languages: ["Python", "TypeScript", "Go", "SQL", "Bash"],
    "Cloud & Infrastructure": ["AWS", "Kubernetes", "Terraform", "Docker", "Vercel", "Neon"],
    "Data & Messaging": ["PostgreSQL", "Redis", "Kafka", "Flink", "ClickHouse"],
  },
  custom_sections: [
    {
      id: "cert-1",
      title: "Certifications",
      kind: "certifications",
      text: "",
      bullets: [],
      items: [
        {
          title: "AWS Certified Solutions Architect, Professional",
          subtitle: "Amazon Web Services",
          location: "",
          start_date: "2024",
          end_date: "",
          detail: "",
          link: "",
          bullets: [],
        },
      ],
    },
  ],
  section_order: ["summary", "experience", "education", "projects", "skills", "technologies", "cert-1"],
};

const issue = (id, title, severity, section, count, evidence) => ({
  id,
  title,
  severity,
  count,
  description:
    "Bullets that describe activity without a result read as job-description filler to " +
    "recruiters, and rank poorly with the ATS models that score your application before a " +
    "human ever opens it.",
  evidence,
  suggestion: "Add a metric: what changed, by how much, over what period.",
  section,
});

const analysisReport = {
  overall_grade: "B-",
  letter_grade: "B",
  score: 74,
  urgent_fix_count: 3,
  critical_fix_count: 2,
  optional_fix_count: 6,
  analyzed_at: "2026-07-10T18:22:00Z",
  summary:
    "Solid engineering resume. The biggest wins are quantifying the remaining bullets and " +
    "tightening the summary so the first line names the role you are actually applying for.",
  highlights: [
    "Three experience bullets carry no measurable outcome",
    "The summary never names the target role",
    "Seventeen flat, unranked skills dilute your strongest signals",
  ],
  strengths: [
    "Clean, ATS-parseable structure with no tables or text boxes",
    "Strong verbs and consistent tense throughout",
    "Education and certifications are current and clearly dated",
  ],
  categories: [
    {
      id: "impact",
      name: "Impact & Quantification",
      score: 68,
      why_it_matters:
        "Recruiters spend about seven seconds on the first pass. Numbers are the only thing that survives that scan.",
      issues: [
        issue("i1", "Three experience bullets have no measurable outcome", "urgent", "experience", 3, [
          "Mentored 4 engineers; introduced trunk-based development and a 9-minute CI pipeline.",
          "Built the ingestion pipeline processing 2.1B events/day with Kafka and Flink.",
        ]),
        issue("i4", "Project bullets describe the stack, not the result", "critical", "projects", 1, [
          "Chrome extension + FastAPI backend that tailors resumes and autofills applications.",
        ]),
      ],
    },
    {
      id: "clarity",
      name: "Clarity & Concision",
      score: 82,
      why_it_matters: "Long bullets get skipped. Two lines is the ceiling.",
      issues: [
        issue("i5", "Two bullets run past three lines at standard margins", "optional", "experience", 2, [
          "Architected and shipped a multi-tenant autofill engine spanning 69 applicant tracking systems, raising successful-fill rate from 61% to 94% across 40,000 monthly applications.",
        ]),
      ],
    },
    {
      id: "keywords",
      name: "Keyword Coverage",
      score: 71,
      why_it_matters: "The ATS matches your text against the posting before a human sees it.",
      issues: [
        issue("i2", "Summary does not name the target role", "critical", "summary", 1, [
          "Backend-leaning full-stack engineer with 5 years building distributed platforms.",
        ]),
        issue("i3", "Skills list is long and unranked", "optional", "skills", 17, SKILLS),
      ],
    },
    {
      id: "format",
      name: "Formatting & ATS Safety",
      score: 90,
      why_it_matters: "Tables, columns and text boxes are the most common causes of a garbled parse.",
      issues: [],
    },
  ],
};

const resumeListItem = (id, name, primary) => ({
  id,
  name,
  target_job_title: id === 1 ? LONG_TITLE : "Backend Engineer",
  is_primary: primary,
  status: "analyzed",
  created_at: "2026-06-02T10:00:00Z",
  updated_at: "2026-07-10T10:00:00Z",
});

// Two, not three. Resume.tsx caps uploads at MAX_RESUME_SLOTS = 3, so a full
// list renders "Add Resume" *disabled*, the upload-modal audit state would then
// click a dead button, never open the modal, and silently re-measure the list
// underneath it. Leaving a free slot keeps that screen reachable.
const resumes = [
  resumeListItem(1, "Wissam_Elmasry_Resume_Senior_Platform_Engineer_v7_FINAL.pdf", true),
  resumeListItem(2, "Resume, Backend.pdf", false),
];

const resumeDetail = {
  id: 1,
  name: "Wissam_Elmasry_Resume_Senior_Platform_Engineer_v7_FINAL.pdf",
  status: "analyzed",
  is_primary: true,
  created_at: "2026-06-02T10:00:00Z",
  updated_at: "2026-07-10T10:00:00Z",
  content_updated_at: "2026-07-10T10:00:00Z",
  target_job_title: LONG_TITLE,
  profile,
  analysis_report: analysisReport,
  document: null,
};

const applications = Array.from({ length: 8 }, (_, i) => ({
  id: i + 1,
  platform: i % 2 ? "greenhouse" : "workday",
  company: i === 0 ? LONG_COMPANY : `Acme Corp ${i + 1}`,
  role: i === 0 ? LONG_TITLE : `Software Engineer ${i + 1}`,
  url: LONG_URL,
  status: ["applied", "interviewing", "rejected", "offer"][i % 4],
  applied_at: "2026-07-0" + ((i % 9) + 1) + "T14:00:00Z",
  notes: i === 0 ? "Referred by a former colleague on the platform team; follow up in one week." : null,
  resume_version: "v7, Senior Platform Engineer",
  company_logo: null,
  company_domain: "example.com",
  company_url: "https://example.com",
}));

const settings = {
  first_name: "Wissam",
  last_name: "Elmasry",
  email: LONG_EMAIL,
  phone: "+1 (416) 555-0142",
  linkedin_url: "https://www.linkedin.com/in/wissam-elmasry-software-engineer-toronto",
  website: "https://portfolio.wissamelmasry.example.com/case-studies/platform",
  job_title: LONG_TITLE,
  location: "Toronto, Ontario, Canada",
  remote_only: true,
  prefilled_answers: {
    "Are you legally authorized to work in Canada?": "Yes",
    "Will you now or in the future require sponsorship for employment visa status?": "No",
    "Describe, in detail, why you are interested in this specific role at this company": Array(4)
      .fill("I have followed the platform team's engineering blog for two years.")
      .join(" "),
  },
  resume_uploaded: true,
  resume_file_name: "Wissam_Elmasry_Resume_Senior_Platform_Engineer_v7_FINAL.pdf",
  pause_before_submit: true,
  smooth_scrolling: true,
  follow_companies: false,
};

const sessions = [
  {
    sid: "sess_1",
    client: "web",
    created_at: "2026-07-10T09:00:00Z",
    last_seen_at: "2026-07-11T08:12:00Z",
    last_ip: "203.0.113.42",
    user_agent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    is_current: true,
  },
  {
    sid: "sess_2",
    client: "extension",
    created_at: "2026-07-02T09:00:00Z",
    last_seen_at: "2026-07-09T21:44:00Z",
    last_ip: "198.51.100.7",
    user_agent: "Chrome Extension 0.4.0 on macOS 15.2",
    is_current: false,
  },
];

// GET /api/user/application-profile. Mirror `ApplicationProfileOut`
// (backend/routers/profile.py) exactly, camelCase, nested `eeo`.
//
// This fixture was snake_case until 2026-07-14, which meant /app/profile
// audited an essentially EMPTY page: Profile.tsx reads `addressStreet`,
// `currentTitle`, `eeo.gender` and friends, found none of them, and rendered
// placeholder dashes. The audit dutifully reported 0/0, on nothing. If you
// change the endpoint's shape, change this too, or the audit quietly stops
// measuring the page.
//
// Values are worst-case on purpose. The extension writes back the *literal
// option text* it harvested from a real form, so `workAuthorization` is a full
// sentence, the widest string on the page and the real 320px overflow risk.
const applicationProfile = {
  firstName: "Wissam",
  lastName: "Elmasry",
  email: LONG_EMAIL,
  phone: "+1 (416) 555-0142",
  location: "Toronto, Ontario, Canada",
  addressStreet: "1234 Bloor Street West, Suite 1800",
  addressCity: "Toronto",
  addressState: "Ontario",
  postalCode: "M6H 1M9",
  country: "Canada",
  linkedin: "https://www.linkedin.com/in/wissam-elmasry-software-engineer-toronto",
  github: "https://github.com/wissam-elmasry-platform",
  portfolio: "https://portfolio.wissamelmasry.example.com/case-studies/platform",
  currentCompany: "Shopify",
  currentTitle: "Software Engineer Intern, Platform Infrastructure",
  workAuthorization:
    "Yes, I am legally authorized to work in the United States for any employer without sponsorship now or in the future",
  requiresSponsorship: "No, I will not require sponsorship for employment visa status",
  salaryExpectation: "$120,000 - $165,000 CAD",
  eeo: {
    gender: "Male",
    race: "Prefer not to say",
    hispanicLatino: "No",
    veteranStatus: "I am not a protected veteran",
    disabilityStatus: "No, I do not have a disability",
  },
};

const githubSources = [
  {
    id: 1,
    repo_url: "https://github.com/SimplifyJobs/Summer2026-Internships",
    label: "Summer 2026 Internships",
    enabled: true,
    last_polled_at: "2026-07-11T06:00:00Z",
    jobs_found: 812,
    status: "ok",
  },
];

/* ---- AI flow (CustomResumeModal / CoverLetterModal / the /embed pages) ---- */

const customResumeAnalysis = {
  overall_score: 88,
  ats_score: 91,
  match_label: "Strong Match",
  keyword_coverage: 0.82,
  matched_keywords: [
    "Python", "TypeScript", "Kubernetes", "PostgreSQL", "Distributed Systems",
    "CI/CD", "Terraform", "AWS", "Microservices",
  ],
  missing_keywords: [
    "Apache Flink", "Service Mesh (Istio)", "OpenTelemetry", "gRPC", "Protocol Buffers",
  ],
  strengths: [
    "Backend depth matches the platform team's core stack",
    "Quantified impact on latency and fill rate",
  ],
  weaknesses: ["No explicit service-mesh experience", "Observability tooling is not named"],
  suggestions: [
    "Name OpenTelemetry explicitly in the Northbound Analytics bullet. You built the tracing.",
    "Add gRPC to the technologies list; the posting mentions it three times.",
  ],
};

const customResume = {
  profile,
  analysis: customResumeAnalysis,
  changes: [
    "Rewrote the summary to open with the target title",
    "Added Kubernetes and Terraform to the skills list",
    "Quantified three experience bullets",
  ],
  keywords_added: ["Kubernetes", "Terraform", "gRPC"],
  resume_id: 1,
};

const coverLetter = {
  text:
    "Dear Hiring Manager,\n\n" +
    "I am writing to apply for the Senior Software Development Engineer II role on the " +
    "Distributed Systems & Platform Infrastructure team at " + LONG_COMPANY + ".\n\n" +
    "Over the last five years I have built the kind of platform your posting describes: at my " +
    "current role I architected a multi-tenant autofill engine spanning 69 applicant tracking " +
    "systems, raising successful-fill rate from 61% to 94% across 40,000 monthly applications, " +
    "and led the migration from a monolith to twelve event-driven services on Kubernetes that " +
    "cut p99 latency from 340ms to 88ms.\n\n" +
    "I would welcome the chance to talk about what your platform team is building next.\n\n" +
    "Sincerely,\nWissam Elmasry",
  cover_letter: "",
};

/* Admin feedback console. Long message and long email on purpose: the console
   renders whatever a user typed, so wrapping is the thing worth auditing. */
const feedbackItems = [
  {
    id: 3,
    user_id: "44",
    email: LONG_EMAIL,
    category: "bug_report",
    message:
      "The autofill typed my employer's name into the box that asks me to certify " +
      "the information is accurate, and it reported the field as filled. I had to " +
      "clear it by hand before the application would submit.",
    created_at: "2026-08-12T15:04:00",
  },
  {
    id: 2,
    user_id: "12",
    email: "student@school.edu",
    category: "feature_request",
    message: "Could you add a way to sort saved jobs by application deadline?",
    created_at: "2026-08-11T09:20:00",
  },
  {
    id: 1,
    user_id: "",
    email: null,
    category: "ux_feedback",
    message: "Love it.",
    created_at: "2026-08-10T18:45:00",
  },
];

const feedback = { items: feedbackItems, total: 128 };

// GET /jobs/sidebar, the Jobs page right rail. Worst case: long names everywhere.
const sidebar = {
  resume: {
    id: 42, name: "Software Engineering Resume, Fall 2026 (tailored for backend roles)",
    scored_jobs: 339, avg_match: 64, strong_matches: 23, gap_pool_size: 120,
  },
  resume_skills: ["python", "typescript", "react", "fastapi", "postgresql", "docker", "aws"],
  skill_gaps: [
    { skill: "kubernetes", job_count: 41 },
    { skill: "machine learning", job_count: 27 },
    { skill: "terraform", job_count: 12 },
  ],
  progress: { week_start: "2026-10-05T00:00:00Z", applied_week: 4, applied_total: 37, saved_week: 6, interviews: 2 },
  closing_soon: [
    { id: 1, title: LONG_TITLE, company: LONG_COMPANY, company_logo: "", company_domain: "", company_url: "", age_days: 9, reason: "stale" },
    { id: 2, title: "Software Engineer, New Grad", company: "Shopify", company_logo: "", company_domain: "shopify.com", company_url: "", age_days: 26, reason: "old" },
  ],
  feed: { total: 4373, new_since: 1284, since: "2026-10-07T00:00:00Z", remote: 512, strong_matches: 23 },
  top_companies: [
    { company: LONG_COMPANY, count: 14, company_logo: "", company_domain: "", company_url: "" },
    { company: "Shopify", count: 9, company_logo: "", company_domain: "shopify.com", company_url: "" },
    { company: "RBC", count: 7, company_logo: "", company_domain: "rbc.com", company_url: "" },
    { company: "Wealthsimple", count: 5, company_logo: "", company_domain: "wealthsimple.com", company_url: "" },
  ],
  top_companies_basis: "matches",
  autofill: { fields_filled: 1234, passes: 41 },
  alerts_enabled: true,
};

module.exports = {
  matchBreakdown,
  user,
  feedback,
  sidebar,
  jobs,
  stats,
  profile,
  analysisReport,
  resumes,
  resumeDetail,
  applications,
  settings,
  sessions,
  applicationProfile,
  githubSources,
  customResume,
  customResumeAnalysis,
  coverLetter,
  LONG_TITLE,
  LONG_COMPANY,
  LONG_EMAIL,
  LONG_URL,
};
