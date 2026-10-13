import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { render, screen, cleanup } from "@testing-library/react";
import React from "react";
import fs from "fs";
import path from "path";
import { ApplyTrackingProvider } from "../context/ApplyTracking";

function renderWithProviders(ui: React.ReactElement) {
  return render(
    <MemoryRouter>
      <ApplyTrackingProvider>{ui}</ApplyTrackingProvider>
    </MemoryRouter>
  );
}

/**
 * Unit tests for job-detail-inline-panel layout and accessibility.
 * Task 6.7
 */

// --- Mock job object ---
const mockJob = {
  id: 1,
  title: "Senior Software Engineer",
  company: "TechCorp",
  location: "San Francisco, CA",
  url: "https://example.com/job/1",
  description: "We are looking for a senior engineer with extensive experience in building scalable systems.",
  match_score: 85,
  match_label: "STRONG MATCH",
  experience_score: 90,
  skill_score: 80,
  industry_score: 85,
  applicant_count: 42,
  source_platform: "linkedin",
  scraped_at: new Date(Date.now() - 3600000).toISOString(),
  salary_range: "$150k-$200k",
  status: "new",
  company_logo: "",
  work_type: "remote",
  role_category: "Engineering",
  country: "US",
  experience_level: "new_grad",
  posted_date: null,
};

// --- Mock JobFilterBar to avoid its internal complexity ---
vi.mock("../components/JobFilterBar", () => ({
  default: () => <div data-testid="job-filter-bar" />,
}));

describe("Job Detail Inline Panel - Unit Tests (Task 6.7)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Mock global.fetch to prevent API calls
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve([]),
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  /**
   * Test: Default state renders full-width list without detail panel
   * Validates: Requirement 1.4
   */
  it("default state renders full-width list without detail panel (Req 1.4)", async () => {
    const { default: Jobs } = await import("../pages/Jobs");
    const { container } = renderWithProviders(<Jobs />);

    // The content area should exist
    const contentArea = container.querySelector(".jobs-content-area");
    expect(contentArea).not.toBeNull();

    // No has-detail class when no job is selected
    expect(contentArea!.classList.contains("has-detail")).toBe(false);

    // No detail panel should be rendered
    const detailPanel = container.querySelector(".job-detail-inline");
    expect(detailPanel).toBeNull();
  });

  /**
   * Test: Split layout applies ~40%/60% width classes
   * Validates: Requirement 5.1
   *
   * Since jsdom doesn't compute CSS, we verify the CSS file contains the correct rules.
   */
  it("split layout CSS defines ~40%/60% width rules (Req 5.1)", () => {
    const cssPath = path.resolve(__dirname, "../index.css");
    const cssContent = fs.readFileSync(cssPath, "utf-8");

    // Verify the 40% width rule for jobs-feed when has-detail is active
    expect(cssContent).toContain(".jobs-content-area.has-detail .jobs-feed");
    expect(cssContent).toMatch(/width:\s*40%/);

    // Verify the 60% width rule for job-detail-inline
    expect(cssContent).toContain(".job-detail-inline");
    expect(cssContent).toMatch(/width:\s*60%/);
  });

  /**
   * Test: Both panels have independent scroll
   * Validates: Requirements 5.2, 5.3
   *
   * Verify CSS rules exist for overflow-y: auto on both panels.
   */
  it("both panels have independent scroll via overflow-y: auto in CSS (Req 5.2, 5.3)", () => {
    const cssPath = path.resolve(__dirname, "../index.css");
    const cssContent = fs.readFileSync(cssPath, "utf-8");

    // Check that jobs-feed has overflow-y: auto
    const jobsFeedSection = cssContent.substring(
      cssContent.indexOf(".jobs-content-area .jobs-feed")
    );
    expect(jobsFeedSection).toMatch(/overflow-y:\s*auto/);

    // Check that job-detail-inline has overflow-y: auto
    const detailInlineSection = cssContent.substring(
      cssContent.indexOf(".job-detail-inline {")
    );
    expect(detailInlineSection).toMatch(/overflow-y:\s*auto/);
  });

  /**
   * Test: Close button is focusable with aria-label
   * Validates: Requirement 7.2
   */
  it("close button is a focusable <button> with aria-label (Req 7.2)", async () => {
    const { default: JobDetailView } = await vi.importActual<
      typeof import("../components/JobDetailView")
    >("../components/JobDetailView");

    renderWithProviders(<JobDetailView job={mockJob as any} onClose={() => {}} />);

    // Find the close button by aria-label
    const closeButton = screen.getByRole("button", { name: "Close detail panel" });
    expect(closeButton).toBeInTheDocument();

    // It should be a native <button> element (inherently focusable)
    expect(closeButton.tagName).toBe("BUTTON");

    // Verify aria-label attribute
    expect(closeButton).toHaveAttribute("aria-label", "Close detail panel");
  });

  /**
   * Test: No job-detail-overlay element exists
   * Validates: Requirement 2.2
   */
  it("no job-detail-overlay element exists in the rendered output (Req 2.2)", async () => {
    const { default: Jobs } = await import("../pages/Jobs");
    const { container } = renderWithProviders(<Jobs />);

    // No overlay element should exist
    const overlay = container.querySelector(".job-detail-overlay");
    expect(overlay).toBeNull();
  });

  /**
   * Test: "Apply with Tailrd" and "View Original Post" buttons render
   * Validates: Requirement 3.2
   */
  it('"Apply with Tailrd" and "View Original Post" buttons render (Req 3.2)', async () => {
    const { default: JobDetailView } = await vi.importActual<
      typeof import("../components/JobDetailView")
    >("../components/JobDetailView");

    renderWithProviders(<JobDetailView job={mockJob as any} onClose={() => {}} />);

    // Check for "Apply with Tailrd" button/link
    const applyButton = screen.getByText(/Apply with Tailrd/i);
    expect(applyButton).toBeInTheDocument();

    // Check for "View Original Post" button/link
    const viewOriginalButton = screen.getByText(/View Original Post/i);
    expect(viewOriginalButton).toBeInTheDocument();
  });

  /**
   * Test: AI Tools Sidebar renders all three tool buttons
   * Validates: Requirement 3.6
   */
  it("AI Tools Sidebar renders its tools, and no AI fit analysis (Req 3.6, Task 9.4)", async () => {
    const { default: AIToolsSidebar } = await vi.importActual<
      typeof import("../components/AIToolsSidebar")
    >("../components/AIToolsSidebar");

    render(<AIToolsSidebar jobId={1} />);

    expect(screen.getByText("Customize Your Resume")).toBeInTheDocument();
    expect(screen.getByText("Build Cover Letter")).toBeInTheDocument();
    // /ai/analyze-fit is gone (404): fit comes from the engine in the detail view.
    expect(screen.queryByText("Analyze How Well You Fit")).toBeNull();
  });
});
