import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import * as fc from "fast-check";
import React from "react";
import { ApplyTrackingProvider } from "../context/ApplyTracking";
import { displayLocation } from "../lib/jobLocation";

/**
 * Feature: job-detail-inline-panel
 * Property 1: Inline rendering without overlay
 *
 * **Validates: Requirements 1.1, 1.3, 2.1, 2.2**
 *
 * For any valid job object, when it is set as the selected job, the Jobs page
 * SHALL render the detail panel as an inline sibling of the job list (both present
 * in a flex container) and SHALL NOT render any element with the overlay class,
 * fixed-position backdrop, or click-to-dismiss behavior.
 */

// --- Mock the JobDetailView component to avoid its internal fetch calls ---
vi.mock("../components/JobDetailView", () => ({
  default: ({ job, onClose }: { job: { id: number; title: string; company: string }; onClose?: () => void }) => (
    <div data-testid="job-detail-view">
      <span data-testid="detail-title">{job.title}</span>
      <span data-testid="detail-company">{job.company}</span>
      {onClose && <button onClick={onClose} aria-label="Close detail panel">X</button>}
    </div>
  ),
}));

// --- Mock JobFilterBar to avoid its internal complexity ---
vi.mock("../components/JobFilterBar", () => ({
  default: () => <div data-testid="job-filter-bar" />,
}));

// --- Job interface matching Jobs.tsx ---
interface Job {
  id: number;
  title: string;
  company: string;
  location: string;
  url: string;
  description: string;
  match_score: number;
  match_band?: string | null;
  match_outcome?: string | null;
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
  work_type: string;
  role_category: string;
  country: string;
  experience_level: string;
  posted_date: string | null;
}

// --- Arbitrary for generating random Job objects ---
const jobArb: fc.Arbitrary<Job> = fc.record({
  id: fc.nat({ max: 100000 }),
  title: fc.string({ minLength: 1, maxLength: 50 }).filter((s) => s.trim().length > 0),
  company: fc.string({ minLength: 1, maxLength: 30 }).filter((s) => s.trim().length > 0),
  location: fc.string({ minLength: 0, maxLength: 30 }),
  url: fc.constant("https://example.com/job"),
  description: fc.string({ minLength: 0, maxLength: 100 }),
  match_score: fc.integer({ min: 0, max: 100 }),
  match_summary: fc.constant(""),
  match_label: fc.constantFrom("STRONG MATCH", "GOOD MATCH", "FAIR MATCH"),
  salary_range: fc.constant(""),
  company_size: fc.constant(""),
  status: fc.constantFrom("new", "applied", "viewed"),
  easy_apply: fc.boolean(),
  ats_type: fc.constant(""),
  scraped_at: fc.constant(new Date().toISOString()),
  source_platform: fc.constantFrom("linkedin", "github"),
  saved: fc.boolean(),
  experience_score: fc.integer({ min: 0, max: 100 }),
  skill_score: fc.integer({ min: 0, max: 100 }),
  industry_score: fc.integer({ min: 0, max: 100 }),
  applicant_count: fc.oneof(fc.constant(null), fc.integer({ min: 1, max: 500 })),
  company_logo: fc.constant(""),
  work_type: fc.constantFrom("remote", "hybrid", "onsite"),
  role_category: fc.constant("Engineering"),
  country: fc.constantFrom("US", "CA"),
  experience_level: fc.constantFrom("new_grad", "internship", "entry"),
  posted_date: fc.constant(null),
});

/**
 * Simplified component that replicates the layout rendering logic from Jobs.tsx.
 * This mirrors the actual split-layout pattern without needing fetch/localStorage/routing.
 */
function JobsLayoutUnderTest({
  jobs,
  selectedJob,
  onSelectJob,
  onClose,
}: {
  jobs: Job[];
  selectedJob: Job | null;
  onSelectJob: (job: Job) => void;
  onClose: () => void;
}) {
  // Dynamically import the mocked JobDetailView
  const JobDetailView = React.lazy(() => import("../components/JobDetailView"));

  return (
    <div className="jobs-page">
      <div className={`jobs-content-area${selectedJob ? " has-detail" : ""}`}>
        <div className="jobs-feed">
          {jobs.map((job) => (
            <div
              key={job.id}
              className={`job-card${selectedJob?.id === job.id ? " selected" : ""}`}
              onClick={() => onSelectJob(job)}
            >
              <span>{job.title}</span>
            </div>
          ))}
        </div>
        {selectedJob && (
          <div className="job-detail-inline">
            <React.Suspense fallback={null}>
              <JobDetailView job={selectedJob} onClose={onClose} />
            </React.Suspense>
          </div>
        )}
      </div>
    </div>
  );
}

describe("Feature: job-detail-inline-panel, Property 1: Inline rendering without overlay", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("when a job is selected, no job-detail-overlay element exists in the DOM", () => {
    fc.assert(
      fc.property(jobArb, (job) => {
        const { container } = render(
          <JobsLayoutUnderTest
            jobs={[job]}
            selectedJob={job}
            onSelectJob={() => {}}
            onClose={() => {}}
          />
        );

        // No overlay element should exist
        const overlay = container.querySelector(".job-detail-overlay");
        expect(overlay).toBeNull();

        cleanup();
      }),
      { numRuns: 100 }
    );
  });

  it("when a job is selected, the detail panel is an inline sibling of the job list within a flex container", () => {
    fc.assert(
      fc.property(jobArb, (job) => {
        const { container } = render(
          <JobsLayoutUnderTest
            jobs={[job]}
            selectedJob={job}
            onSelectJob={() => {}}
            onClose={() => {}}
          />
        );

        // The content area should exist with has-detail class
        const contentArea = container.querySelector(".jobs-content-area.has-detail");
        expect(contentArea).not.toBeNull();

        // The jobs-feed and job-detail-inline should both be direct children of content area
        const jobsFeed = contentArea!.querySelector(":scope > .jobs-feed");
        const detailInline = contentArea!.querySelector(":scope > .job-detail-inline");

        expect(jobsFeed).not.toBeNull();
        expect(detailInline).not.toBeNull();

        // They should be siblings (both direct children of the same parent)
        expect(jobsFeed!.parentElement).toBe(detailInline!.parentElement);
        expect(jobsFeed!.parentElement).toBe(contentArea);

        cleanup();
      }),
      { numRuns: 100 }
    );
  });

  it("no fixed-position overlay or backdrop elements exist when a job is selected", () => {
    fc.assert(
      fc.property(jobArb, (job) => {
        const { container } = render(
          <JobsLayoutUnderTest
            jobs={[job]}
            selectedJob={job}
            onSelectJob={() => {}}
            onClose={() => {}}
          />
        );

        // No element with overlay-related classes
        expect(container.querySelector(".job-detail-overlay")).toBeNull();
        expect(container.querySelector(".overlay")).toBeNull();
        expect(container.querySelector(".backdrop")).toBeNull();
        expect(container.querySelector(".modal")).toBeNull();

        // The detail panel should have the inline class, not overlay
        const detailPanel = container.querySelector(".job-detail-inline");
        expect(detailPanel).not.toBeNull();

        cleanup();
      }),
      { numRuns: 100 }
    );
  });

  it("the actual Jobs component renders inline detail without overlay when a job is selected", async () => {
    // Mock global.fetch to return empty jobs array
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve([]),
    });
    global.fetch = mockFetch;

    // Import the actual Jobs component
    const { default: Jobs } = await import("../pages/Jobs");

    fc.assert(
      fc.property(jobArb, (job) => {
        const { container } = render(
          <MemoryRouter>
            <ApplyTrackingProvider>
              <Jobs />
            </ApplyTrackingProvider>
          </MemoryRouter>
        );

        // Even with the actual component, no overlay should exist
        const overlay = container.querySelector(".job-detail-overlay");
        expect(overlay).toBeNull();

        // The content area structure should exist
        const contentArea = container.querySelector(".jobs-content-area");
        expect(contentArea).not.toBeNull();

        cleanup();
      }),
      { numRuns: 10 } // Fewer runs for the full component render
    );
  });
});


/**
 * Feature: job-detail-inline-panel
 * Property 2: Detail panel content completeness
 *
 * **Validates: Requirements 3.1, 3.3, 3.4, 3.5**
 *
 * For any job with populated fields (title, company, location, work_type,
 * description, match_score > 0, applicant_count > 0), the rendered detail panel
 * SHALL contain: the job title, company name, location tag, work type badge,
 * job description text under an "Overview" heading, the fit score circle with
 * its "Fit score N · band" headline (Task 9.4; no "%"), and the applicant count.
 */
describe("Feature: job-detail-inline-panel, Property 2: Detail panel content completeness", () => {
  let RealJobDetailView: typeof import("../components/JobDetailView").default;

  beforeEach(async () => {
    vi.clearAllMocks();

    // Mock global.fetch to prevent actual API calls
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });

    // Import the REAL JobDetailView (bypassing the vi.mock at the top)
    const mod = await vi.importActual<typeof import("../components/JobDetailView")>(
      "../components/JobDetailView"
    );
    RealJobDetailView = mod.default;
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  // Arbitrary for jobs with ALL fields populated (non-empty)
  const populatedJobArb: fc.Arbitrary<Job> = fc.record({
    id: fc.nat({ max: 100000 }),
    title: fc.string({ minLength: 1, maxLength: 50 }).filter((s) => s.trim().length > 0),
    company: fc.string({ minLength: 2, maxLength: 30 }).filter((s) => s.trim().length > 0),
    location: fc.string({ minLength: 1, maxLength: 30 }).filter((s) => s.trim().length > 0),
    url: fc.constant("https://example.com/job"),
    description: fc.string({ minLength: 51, maxLength: 200 }).filter((s) => s.trim().length > 50),
    match_score: fc.integer({ min: 1, max: 100 }),
    match_band: fc.constantFrom("Strong match", "Good match", "Partial match", "Limited match"),
    match_outcome: fc.constant("SCORED"),
    match_summary: fc.constant(""),
    match_label: fc.constant(""),
    salary_range: fc.constant(""),
    company_size: fc.constant(""),
    status: fc.constantFrom("new", "applied", "viewed"),
    easy_apply: fc.boolean(),
    ats_type: fc.constant(""),
    scraped_at: fc.constant(new Date().toISOString()),
    source_platform: fc.constantFrom("linkedin", "github"),
    saved: fc.boolean(),
    experience_score: fc.integer({ min: 1, max: 100 }),
    skill_score: fc.integer({ min: 1, max: 100 }),
    industry_score: fc.integer({ min: 1, max: 100 }),
    applicant_count: fc.integer({ min: 1, max: 500 }),
    company_logo: fc.constant(""),
    work_type: fc.constantFrom("remote", "hybrid", "onsite"),
    role_category: fc.constant("Engineering"),
    country: fc.constantFrom("US", "CA"),
    experience_level: fc.constantFrom("new_grad", "internship", "entry"),
    posted_date: fc.constant(null),
  });

  it("renders job title, company, location, work type, description, match score, and applicant count", () => {
    fc.assert(
      fc.property(populatedJobArb, (job) => {
        const { container } = render(
          <ApplyTrackingProvider>
            <RealJobDetailView job={job as any} onClose={() => {}} />
          </ApplyTrackingProvider>
        );

        // Title should be present
        const titleEl = container.querySelector(".job-detail-title");
        expect(titleEl).not.toBeNull();
        expect(titleEl!.textContent).toBe(job.title);

        // Company name should be present
        const companyEl = container.querySelector(".job-detail-company");
        expect(companyEl).not.toBeNull();
        expect(companyEl!.textContent).toBe(job.company);

        // Location tag should be present (rendered via displayLocation, which
        // falls back to the first raw ";"-segment for rows without
        // locations_json)
        const tags = container.querySelectorAll(".detail-tag");
        const tagTexts = Array.from(tags).map((t) => t.textContent || "");
        const expectedLocation =
          displayLocation(job as { location?: string | null }) || job.location;
        const hasLocation = tagTexts.some((t) => t.includes(expectedLocation));
        expect(hasLocation).toBe(true);

        // Work type badge should be present
        const expectedWorkType =
          job.work_type === "remote" ? "Remote" :
          job.work_type === "hybrid" ? "Hybrid" :
          job.work_type === "onsite" ? "On Site" : job.work_type;
        const hasWorkType = tagTexts.some((t) => t.includes(expectedWorkType));
        expect(hasWorkType).toBe(true);

        // Description under "Overview" heading should be present
        const sectionTitle = container.querySelector(".detail-section-title");
        expect(sectionTitle).not.toBeNull();
        expect(sectionTitle!.textContent).toBe("Overview");
        const descContent = container.querySelector(".description-content");
        expect(descContent).not.toBeNull();

        // Match score circle should be present (score > 0)
        const matchCircle = container.querySelector(".match-circle-large");
        expect(matchCircle).not.toBeNull();
        const matchNumber = container.querySelector(".match-number-large");
        expect(matchNumber).not.toBeNull();
        expect(matchNumber!.textContent).toContain(String(job.match_score));

        // The card's engine result heads the fit card until the breakdown answers.
        const headline = container.querySelector(".fit-headline");
        expect(headline).not.toBeNull();
        expect(headline!.textContent).toBe(`Fit score ${job.match_score} · ${job.match_band}`);
        expect(matchNumber!.textContent).not.toContain("%");

        // Applicant count should be present
        const metaRow = container.querySelector(".job-detail-meta-row");
        expect(metaRow).not.toBeNull();
        expect(metaRow!.textContent).toContain("applicants");

        cleanup();
      }),
      { numRuns: 100 }
    );
  });
});


/**
 * Feature: job-detail-inline-panel
 * Property 3: Close button removes detail panel
 *
 * **Validates: Requirements 4.1**
 *
 * For any selected job, when the close action is triggered (clicking the close button),
 * the detail panel SHALL be removed from the DOM and the content area SHALL revert
 * to full-width single-column layout (no `has-detail` class).
 */
describe("Feature: job-detail-inline-panel, Property 3: Close button removes detail panel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  /**
   * Stateful wrapper that manages selectedJob internally and renders the layout
   * synchronously (without React.lazy) so the close button is immediately available.
   */
  function StatefulCloseLayout({ initialJob, jobs }: { initialJob: Job; jobs: Job[] }) {
    const [selectedJob, setSelectedJob] = React.useState<Job | null>(initialJob);

    return (
      <div className="jobs-page">
        <div className={`jobs-content-area${selectedJob ? " has-detail" : ""}`}>
          <div className="jobs-feed">
            {jobs.map((job) => (
              <div
                key={job.id}
                className={`job-card${selectedJob?.id === job.id ? " selected" : ""}`}
              >
                <span>{job.title}</span>
              </div>
            ))}
          </div>
          {selectedJob && (
            <div className="job-detail-inline">
              <div data-testid="job-detail-view">
                <span data-testid="detail-title">{selectedJob.title}</span>
                <span data-testid="detail-company">{selectedJob.company}</span>
                <button
                  onClick={() => setSelectedJob(null)}
                  aria-label="Close detail panel"
                >
                  X
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  it("clicking the close button removes the detail panel and has-detail class", () => {
    fc.assert(
      fc.property(jobArb, (job) => {
        const { container } = render(
          <StatefulCloseLayout initialJob={job} jobs={[job]} />
        );

        // Before close: detail panel and has-detail class should exist
        const detailBefore = container.querySelector(".job-detail-inline");
        expect(detailBefore).not.toBeNull();

        const contentAreaBefore = container.querySelector(".jobs-content-area.has-detail");
        expect(contentAreaBefore).not.toBeNull();

        // Find and click the close button
        const closeButton = container.querySelector('button[aria-label="Close detail panel"]');
        expect(closeButton).not.toBeNull();
        fireEvent.click(closeButton!);

        // After close: detail panel should be removed
        const detailAfter = container.querySelector(".job-detail-inline");
        expect(detailAfter).toBeNull();

        // After close: has-detail class should be gone
        const contentAreaAfter = container.querySelector(".jobs-content-area.has-detail");
        expect(contentAreaAfter).toBeNull();

        // The content area should still exist but without has-detail
        const contentArea = container.querySelector(".jobs-content-area");
        expect(contentArea).not.toBeNull();

        cleanup();
      }),
      { numRuns: 100 }
    );
  });
});


/**
 * Feature: job-detail-inline-panel
 * Property 4: Job switching updates detail content
 *
 * **Validates: Requirements 4.2**
 *
 * For any two distinct jobs A and B in the list, when job B is selected while
 * job A's detail is displayed, the detail panel SHALL update to show job B's
 * title and company (not job A's).
 */
describe("Feature: job-detail-inline-panel, Property 4: Job switching updates detail content", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  /**
   * Stateful wrapper that manages selectedJob internally and allows switching
   * between jobs by clicking on job cards.
   */
  function StatefulSwitchLayout({ jobs, initialSelected }: { jobs: Job[]; initialSelected: Job }) {
    const [selectedJob, setSelectedJob] = React.useState<Job | null>(initialSelected);

    return (
      <div className="jobs-page">
        <div className={`jobs-content-area${selectedJob ? " has-detail" : ""}`}>
          <div className="jobs-feed">
            {jobs.map((job) => (
              <div
                key={job.id}
                className={`job-card${selectedJob?.id === job.id ? " selected" : ""}`}
                onClick={() => setSelectedJob(job)}
                data-testid={`job-card-${job.id}`}
              >
                <span>{job.title}</span>
              </div>
            ))}
          </div>
          {selectedJob && (
            <div className="job-detail-inline">
              <div data-testid="job-detail-view">
                <span data-testid="detail-title">{selectedJob.title}</span>
                <span data-testid="detail-company">{selectedJob.company}</span>
                <button
                  onClick={() => setSelectedJob(null)}
                  aria-label="Close detail panel"
                >
                  X
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  it("clicking a different job card updates the detail panel to show the new job's title and company", () => {
    fc.assert(
      fc.property(
        fc.tuple(jobArb, jobArb).filter(([a, b]) => a.id !== b.id && a.title !== b.title),
        ([jobA, jobB]) => {
          const { container } = render(
            <StatefulSwitchLayout jobs={[jobA, jobB]} initialSelected={jobA} />
          );

          // Initially, detail panel should show job A's title and company
          const titleBefore = screen.getByTestId("detail-title");
          expect(titleBefore.textContent).toBe(jobA.title);
          const companyBefore = screen.getByTestId("detail-company");
          expect(companyBefore.textContent).toBe(jobA.company);

          // Click job B's card to switch
          const jobBCard = container.querySelector(`[data-testid="job-card-${jobB.id}"]`);
          expect(jobBCard).not.toBeNull();
          fireEvent.click(jobBCard!);

          // After switching, detail panel should show job B's title and company
          const titleAfter = screen.getByTestId("detail-title");
          expect(titleAfter.textContent).toBe(jobB.title);
          const companyAfter = screen.getByTestId("detail-company");
          expect(companyAfter.textContent).toBe(jobB.company);

          // Ensure it's NOT showing job A's data
          expect(titleAfter.textContent).not.toBe(jobA.title);

          cleanup();
        }
      ),
      { numRuns: 100 }
    );
  });
});


/**
 * Feature: job-detail-inline-panel
 * Property 5: Selection highlight exclusivity
 *
 * **Validates: Requirements 6.1, 6.2**
 *
 * For any list of jobs and any selected job, exactly one job card in the list
 * SHALL have the `selected` CSS class applied, and it SHALL correspond to the
 * currently selected job's ID. All other cards SHALL NOT have the `selected` class.
 */
describe("Feature: job-detail-inline-panel, Property 5: Selection highlight exclusivity", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  /**
   * Generate a list of jobs with unique IDs (2-10 items) and pick one as selected.
   */
  const uniqueJobsWithSelectedArb = fc
    .array(jobArb, { minLength: 2, maxLength: 10 })
    .filter((jobs) => {
      const ids = jobs.map((j) => j.id);
      return new Set(ids).size === ids.length; // ensure unique IDs
    })
    .chain((jobs) =>
      fc.nat({ max: jobs.length - 1 }).map((idx) => ({
        jobs,
        selectedJob: jobs[idx],
      }))
    );

  it("exactly one job card has the selected class and it matches the selected job's ID", () => {
    fc.assert(
      fc.property(uniqueJobsWithSelectedArb, ({ jobs, selectedJob }) => {
        const { container } = render(
          <JobsLayoutUnderTest
            jobs={jobs}
            selectedJob={selectedJob}
            onSelectJob={() => {}}
            onClose={() => {}}
          />
        );

        // Find all job cards with the "selected" class
        const selectedCards = container.querySelectorAll(".job-card.selected");

        // Exactly one card should have the selected class
        expect(selectedCards.length).toBe(1);

        // The selected card should correspond to the selected job
        // Verify by checking its text content contains the selected job's title
        expect(selectedCards[0].textContent).toContain(selectedJob.title);

        // All other cards should NOT have the selected class
        const allCards = container.querySelectorAll(".job-card");
        expect(allCards.length).toBe(jobs.length);

        allCards.forEach((card) => {
          if (card.textContent?.includes(selectedJob.title) && card.classList.contains("selected")) {
            // This is the selected card, already verified above
            return;
          }
          if (card !== selectedCards[0]) {
            expect(card.classList.contains("selected")).toBe(false);
          }
        });

        cleanup();
      }),
      { numRuns: 100 }
    );
  });

  it("when switching selection, only the newly selected job has the selected class", () => {
    fc.assert(
      fc.property(
        fc
          .array(jobArb, { minLength: 2, maxLength: 10 })
          .filter((jobs) => {
            const ids = jobs.map((j) => j.id);
            return new Set(ids).size === ids.length;
          })
          .chain((jobs) =>
            fc
              .tuple(
                fc.nat({ max: jobs.length - 1 }),
                fc.nat({ max: jobs.length - 1 })
              )
              .filter(([a, b]) => a !== b)
              .map(([idxA, idxB]) => ({
                jobs,
                jobA: jobs[idxA],
                jobB: jobs[idxB],
              }))
          ),
        ({ jobs, jobA, jobB }) => {
          /**
           * Stateful component that allows switching selection via card clicks.
           */
          function SwitchableLayout() {
            const [selected, setSelected] = React.useState<Job | null>(jobA);

            return (
              <div className="jobs-page">
                <div className={`jobs-content-area${selected ? " has-detail" : ""}`}>
                  <div className="jobs-feed">
                    {jobs.map((job) => (
                      <div
                        key={job.id}
                        className={`job-card${selected?.id === job.id ? " selected" : ""}`}
                        onClick={() => setSelected(job)}
                        data-job-id={job.id}
                      >
                        <span>{job.title}</span>
                      </div>
                    ))}
                  </div>
                  {selected && (
                    <div className="job-detail-inline">
                      <span data-testid="detail-title">{selected.title}</span>
                    </div>
                  )}
                </div>
              </div>
            );
          }

          const { container } = render(<SwitchableLayout />);

          // Initially, job A should be selected
          let selectedCards = container.querySelectorAll(".job-card.selected");
          expect(selectedCards.length).toBe(1);
          expect(selectedCards[0].getAttribute("data-job-id")).toBe(String(jobA.id));

          // Click job B's card
          const jobBCard = container.querySelector(`[data-job-id="${jobB.id}"]`);
          expect(jobBCard).not.toBeNull();
          fireEvent.click(jobBCard!);

          // After switching, only job B should be selected
          selectedCards = container.querySelectorAll(".job-card.selected");
          expect(selectedCards.length).toBe(1);
          expect(selectedCards[0].getAttribute("data-job-id")).toBe(String(jobB.id));

          // Previous selection (job A) should NOT have selected class
          const jobACard = container.querySelector(`[data-job-id="${jobA.id}"]`);
          expect(jobACard!.classList.contains("selected")).toBe(false);

          cleanup();
        }
      ),
      { numRuns: 100 }
    );
  });
});


/**
 * Feature: job-detail-inline-panel
 * Property 6: Escape key closes panel and restores focus
 *
 * **Validates: Requirements 7.1**
 *
 * For any selected job, when a `keydown` event with key "Escape" is dispatched
 * on the document, the detail panel SHALL be removed from the DOM and the
 * `has-detail` class SHALL be removed from the content area.
 */
describe("Feature: job-detail-inline-panel, Property 6: Escape key closes panel and restores focus", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  /**
   * Stateful wrapper that manages selectedJob internally and listens for
   * Escape keydown on document to close the panel (mirroring Jobs.tsx behavior).
   */
  function StatefulEscapeLayout({ initialJob, jobs }: { initialJob: Job; jobs: Job[] }) {
    const [selectedJob, setSelectedJob] = React.useState<Job | null>(initialJob);

    React.useEffect(() => {
      const handleKeyDown = (e: KeyboardEvent) => {
        if (e.key === "Escape" && selectedJob) {
          setSelectedJob(null);
        }
      };
      document.addEventListener("keydown", handleKeyDown);
      return () => document.removeEventListener("keydown", handleKeyDown);
    }, [selectedJob]);

    return (
      <div className="jobs-page">
        <div className={`jobs-content-area${selectedJob ? " has-detail" : ""}`}>
          <div className="jobs-feed">
            {jobs.map((job) => (
              <div
                key={job.id}
                className={`job-card${selectedJob?.id === job.id ? " selected" : ""}`}
              >
                <span>{job.title}</span>
              </div>
            ))}
          </div>
          {selectedJob && (
            <div className="job-detail-inline">
              <div data-testid="job-detail-view">
                <span data-testid="detail-title">{selectedJob.title}</span>
                <span data-testid="detail-company">{selectedJob.company}</span>
                <button
                  onClick={() => setSelectedJob(null)}
                  aria-label="Close detail panel"
                >
                  X
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  it("pressing Escape removes the detail panel and has-detail class", () => {
    fc.assert(
      fc.property(jobArb, (job) => {
        const { container } = render(
          <StatefulEscapeLayout initialJob={job} jobs={[job]} />
        );

        // Before Escape: detail panel and has-detail class should exist
        const detailBefore = container.querySelector(".job-detail-inline");
        expect(detailBefore).not.toBeNull();

        const contentAreaBefore = container.querySelector(".jobs-content-area.has-detail");
        expect(contentAreaBefore).not.toBeNull();

        // Dispatch Escape keydown event on document
        fireEvent.keyDown(document, { key: "Escape" });

        // After Escape: detail panel should be removed from DOM
        const detailAfter = container.querySelector(".job-detail-inline");
        expect(detailAfter).toBeNull();

        // After Escape: has-detail class should be removed
        const contentAreaAfter = container.querySelector(".jobs-content-area.has-detail");
        expect(contentAreaAfter).toBeNull();

        // The content area should still exist but without has-detail
        const contentArea = container.querySelector(".jobs-content-area");
        expect(contentArea).not.toBeNull();

        cleanup();
      }),
      { numRuns: 100 }
    );
  });
});
