import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { ApplyTrackingProvider } from "../context/ApplyTracking";
import JobDetailView from "../components/JobDetailView";
import { resetLiveChecks } from "../lib/jobListing";

const apiPost = vi.fn();
const apiGet = vi.fn();
vi.mock("../auth/api", () => ({
  default: {
    post: (...args: unknown[]) => apiPost(...args),
    get: (...args: unknown[]) => apiGet(...args),
  },
}));

vi.mock("../lib/resumeCoverage", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/resumeCoverage")>();
  return { ...actual, getPrimaryResumeText: () => Promise.resolve("") };
});

vi.mock("../components/JobFilterBar", () => ({
  default: () => <div data-testid="job-filter-bar" />,
  normalizeExperienceLevels: (v: unknown) => (Array.isArray(v) ? v : []),
}));

function renderWithProviders(ui: ReactElement) {
  return render(
    <MemoryRouter>
      <ApplyTrackingProvider>{ui}</ApplyTrackingProvider>
    </MemoryRouter>,
  );
}

const DESCRIPTION =
  "Build and ship services used by millions of people. Work with a small team on distributed systems.";

const job = {
  id: 41,
  title: "Backend Engineer, New Grad",
  company: "Acme",
  location: "Toronto, ON",
  url: "https://example.com/job/41",
  description: DESCRIPTION,
  match_score: 76,
  match_band: "Good match",
  match_outcome: "SCORED",
  match_label: "Good match",
  experience_score: 0,
  skill_score: 0,
  industry_score: 0,
  applicant_count: null,
  source_platform: "ats",
  scraped_at: new Date(Date.now() - 3_600_000).toISOString(),
  salary_range: "",
  status: "new",
  listing_status: "active",
};

const LEGACY = {
  overall_score: 0,
  experience_score: 0,
  skill_score: 0,
  industry_score: 0,
  match_label: "",
  strengths: [],
  weaknesses: [],
};

const SCORED = {
  ...LEGACY,
  overall_score: 76,
  experience_score: 80,
  skill_score: 70,
  industry_score: 60,
  match_label: "Good match",
  outcome: "SCORED",
  band: "Good match",
  display_score: 76,
  headline: "Fit score 76 · Good match",
  basis: "Better fit than 64% of candidates scored for similar roles",
  strength_items: [
    { label: "4 of 5 required skills", impact: "+0.31", note: "Python, SQL, Docker, Git", code: "REQ" },
  ],
  gaps: [
    { label: "Missing: Terraform (required)", impact: "-0.12", note: "Your AWS work partly covers it", code: "GAP" },
  ],
  to_improve: ["Add a project that uses Terraform"],
  notes: ["Some skills appear only in your skills section."],
  interval: { low: 71, high: 79, display: true },
};

function mockApi(breakdown: unknown, opts: { details?: string } = {}) {
  apiPost.mockImplementation((url: string) => {
    if (url.includes("/ai/match-breakdown/")) return Promise.resolve({ data: breakdown });
    if (url.endsWith("/fetch-details")) {
      return Promise.resolve({ data: { description: opts.details ?? DESCRIPTION } });
    }
    if (url.endsWith("/structure-description")) return Promise.resolve({ data: { sections: [], skills: [] } });
    if (url.endsWith("/check-live")) return new Promise(() => {});
    return Promise.resolve({ data: {} });
  });
}

function fitText(container: HTMLElement): string {
  return container.querySelector(".job-detail-sidebar")?.textContent ?? "";
}

describe("JobDetailView: the engine's result", () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    apiGet.mockResolvedValue({ data: [] });
    resetLiveChecks();
  });

  it("always requests the breakdown, even when the card already carries a score", async () => {
    mockApi(SCORED);
    renderWithProviders(<JobDetailView job={job} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/41"));
  });

  it("requests the job details (which backfill the description) before the breakdown", async () => {
    mockApi(SCORED);
    renderWithProviders(<JobDetailView job={{ ...job, id: 42, description: "" }} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/42"));
    const urls = apiPost.mock.calls.map((c) => String(c[0]));
    const details = urls.indexOf("/jobs/42/fetch-details");
    const breakdown = urls.indexOf("/ai/match-breakdown/42");
    expect(details).toBeGreaterThanOrEqual(0);
    expect(details).toBeLessThan(breakdown);
  });

  it("shows the card's result while the breakdown is on its way", async () => {
    apiPost.mockImplementation(() => new Promise(() => {}));
    const { container } = renderWithProviders(<JobDetailView job={job} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/41"));
    await screen.findByText("Scoring your fit...");
    expect(fitText(container)).toContain("Fit score 76 · Good match");
    expect(fitText(container)).not.toContain("%");
  });

  it("SCORED: headline, basis, strengths and gaps with impacts, to improve, notes and the interval", async () => {
    mockApi(SCORED);
    const { container } = renderWithProviders(<JobDetailView job={job} />);
    expect(await screen.findByText("Fit score 76 · Good match")).toBeTruthy();
    expect(screen.getByText("Better fit than 64% of candidates scored for similar roles")).toBeTruthy();
    expect(screen.getByText("4 of 5 required skills")).toBeTruthy();
    expect(screen.getByText("+0.31")).toBeTruthy();
    expect(screen.getByText("Python, SQL, Docker, Git")).toBeTruthy();
    expect(screen.getByText("Missing: Terraform (required)")).toBeTruthy();
    expect(screen.getByText("-0.12")).toBeTruthy();
    expect(screen.getByText("Your AWS work partly covers it")).toBeTruthy();
    expect(screen.getByText("Add a project that uses Terraform")).toBeTruthy();
    expect(screen.getByText("Some skills appear only in your skills section.")).toBeTruthy();
    expect(screen.getByText("Likely range 71 to 79")).toBeTruthy();
    // The only "%" is the basis sentence's percentile; never "76%" or "% match".
    const text = fitText(container);
    expect(text).not.toMatch(/\d+%\s*match|% match|76%/i);
    // The old 0..100 sub-score bars are gone.
    expect(text).not.toContain("Match Breakdown");
    expect(container.querySelector(".breakdown-bar")).toBeNull();
  });

  it("hides the interval when the engine says not to display it", async () => {
    mockApi({ ...SCORED, interval: { low: 71, high: 79, display: false } });
    renderWithProviders(<JobDetailView job={job} />);
    await screen.findByText("Fit score 76 · Good match");
    expect(screen.queryByText(/Likely range/)).toBeNull();
  });

  it("SCORED_LOW_CONFIDENCE: the band only, never a number", async () => {
    mockApi({
      ...LEGACY,
      match_label: "Partial match",
      outcome: "SCORED_LOW_CONFIDENCE",
      band: "Partial match",
      display_score: null,
      headline: "Partial match",
      strength_items: [{ label: "Python", impact: "", note: "", code: "" }],
      gaps: [],
      to_improve: [],
      notes: ["There isn't enough reliable information to show a score."],
      interval: null,
    });
    const { container } = renderWithProviders(
      <JobDetailView job={{ ...job, match_score: 0, match_band: "Partial match", match_outcome: "SCORED_LOW_CONFIDENCE" }} />,
    );
    expect(await screen.findByText("There isn't enough reliable information to show a score.")).toBeTruthy();
    expect(screen.getAllByText("Partial match").length).toBeGreaterThan(0);
    expect(fitText(container)).not.toMatch(/\d/);
    expect(container.querySelector(".match-circle-large")).toBeNull();
  });

  it("NEEDS_REVIEW: We couldn't score this, then the plain sentence for each reason", async () => {
    mockApi({
      ...LEGACY,
      match_label: "We couldn't score this",
      outcome: "NEEDS_REVIEW",
      band: null,
      display_score: null,
      headline: "We couldn't score this",
      strength_items: [],
      gaps: [],
      to_improve: [],
      notes: [
        "We couldn't find any requirements in this job posting.",
        "None of this posting's requirements are skills we recognise yet.",
      ],
      interval: null,
    });
    const { container } = renderWithProviders(
      <JobDetailView job={{ ...job, match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" }} />,
    );
    expect(await screen.findByText("We couldn't find any requirements in this job posting.")).toBeTruthy();
    expect(screen.getByText("None of this posting's requirements are skills we recognise yet.")).toBeTruthy();
    expect(screen.getAllByText("We couldn't score this").length).toBeGreaterThan(0);
    const text = fitText(container);
    expect(text).not.toMatch(/\d/);
    expect(text).not.toMatch(/error|fail|warning|!/i);
  });

  it("GATE_FAIL: Not eligible with the reason, and no number", async () => {
    mockApi({
      ...LEGACY,
      match_label: "Not eligible",
      outcome: "GATE_FAIL",
      band: null,
      display_score: null,
      headline: "Not eligible: this role doesn't offer visa sponsorship.",
      strength_items: [],
      gaps: [],
      to_improve: [],
      notes: ["This is based on the details in your profile. If they've changed, update them and we'll check again."],
      interval: null,
    });
    const { container } = renderWithProviders(
      <JobDetailView job={{ ...job, match_score: 0, match_band: null, match_outcome: "GATE_FAIL" }} />,
    );
    expect(await screen.findByText("Not eligible: this role doesn't offer visa sponsorship.")).toBeTruthy();
    expect(screen.getByText(/If they've changed, update them/)).toBeTruthy();
    expect(fitText(container)).not.toMatch(/\d/);
  });

  it("ignores a breakdown without an outcome", async () => {
    mockApi({});
    const { container } = renderWithProviders(<JobDetailView job={job} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/41"));
    expect(fitText(container)).toContain("Fit score 76 · Good match");
  });
});

describe("Jobs page card", () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    resetLiveChecks();
    window.history.replaceState({}, "", "/app");
  });

  it(
    "shows the engine's result on each card and a null average is fine",
    async () => {
      apiPost.mockImplementation(() => new Promise(() => {}));
      const base = { ...job, saved: false, match_label: "" };
      const jobs = [
        { ...base, id: 51, title: "Role Scored", match_score: 82, match_band: "Good match", match_outcome: "SCORED" },
        { ...base, id: 52, title: "Role Low", match_score: 0, match_band: "Partial match", match_outcome: "SCORED_LOW_CONFIDENCE" },
        { ...base, id: 53, title: "Role Gate", match_score: 0, match_band: null, match_outcome: "GATE_FAIL" },
        { ...base, id: 54, title: "Role Review", match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" },
        { ...base, id: 55, title: "Role New", match_score: 0, match_band: null, match_outcome: null },
      ];
      apiGet.mockImplementation((url: string) => {
        if (url === "/jobs") return Promise.resolve({ data: jobs });
        if (url === "/jobs/stats") {
          return Promise.resolve({ data: { total: 5, applied: 0, new: 0, avg_match_score: null, saved_count: 0 } });
        }
        return Promise.resolve({ data: [] });
      });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      await waitFor(() => expect(container.querySelectorAll(".jobs-feed .job-card")).toHaveLength(5));
      const cards = Array.from(container.querySelectorAll(".jobs-feed .job-card"));
      const badge = (i: number) => cards[i].querySelector(".fit-badge")?.textContent ?? "";
      expect(badge(0)).toContain("Fit 82");
      expect(badge(0)).toContain("Good match");
      expect(badge(1)).toBe("Partial match");
      expect(badge(2)).toBe("Not eligible");
      expect(badge(3)).toBe("We couldn't score this");
      expect(cards[4].querySelector(".fit-badge")).toBeNull();
      for (const card of cards) expect(card.textContent).not.toMatch(/\d%|STRONG MATCH|GOOD MATCH|FAIR MATCH/);
    },
    20_000,
  );
});
