import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { ApplyTrackingProvider } from "../context/ApplyTracking";
import JobDetailView from "../components/JobDetailView";
import { resetLiveChecks } from "../lib/jobListing";
// Real engine output (backend breakdown_from_result), captured; see its _source.
import REAL from "./fixtures/match-breakdowns.json";

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
  match_score: 64,
  match_band: "Partial match",
  match_outcome: "SCORED",
  match_label: "Partial match",
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

const SCORED = REAL.SCORED;
// The engine never produced a displayed interval on the test calibration, so this variant
// applies the engine's own format to the real SCORED result (matcher/explain/breakdown.py
// headline(): "{label} {low}–{high} · {band}" when interval.display).
const SCORED_RANGE = {
  ...SCORED,
  headline: "Fit score 60–68 · Partial match",
  interval: { low: 60, high: 68, display: true },
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

  it("always requests the breakdown, even when the card already carries a result", async () => {
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
    expect(details).toBeGreaterThanOrEqual(0);
    expect(details).toBeLessThan(urls.indexOf("/ai/match-breakdown/42"));
  });

  it("asks for the details first even when the local description is long (it may be a placeholder)", async () => {
    mockApi(SCORED);
    const long = "This button displays the currently selected search type. ".repeat(5);
    renderWithProviders(<JobDetailView job={{ ...job, id: 43, description: long }} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/43"));
    const urls = apiPost.mock.calls.map((c) => String(c[0]));
    const details = urls.indexOf("/jobs/43/fetch-details");
    expect(details).toBeGreaterThanOrEqual(0);
    expect(details).toBeLessThan(urls.indexOf("/ai/match-breakdown/43"));
  });

  it("shows the card's band, never its number, while the breakdown is on its way", async () => {
    apiPost.mockImplementation(() => new Promise(() => {}));
    const { container } = renderWithProviders(<JobDetailView job={job} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    // The card's number may hide a displayed interval (D-54), so only the band is shown.
    expect(fitText(container)).toContain("Partial match");
    expect(fitText(container)).not.toMatch(/\d/);
  });

  it("SCORED (real engine output): headline, ring, basis, strengths and gaps, no raw impacts", async () => {
    mockApi(SCORED);
    const { container } = renderWithProviders(<JobDetailView job={job} />);
    expect(await screen.findByText("Fit score 64 · Partial match")).toBeTruthy();
    expect(container.querySelector(".match-number-large")?.textContent).toBe("64");
    expect(screen.getByText(SCORED.basis)).toBeTruthy();
    expect(screen.getByText("2 of 2 required skills")).toBeTruthy();
    expect(screen.getByText("Python and SQL (through your PostgreSQL experience)")).toBeTruthy();
    expect(screen.getByText("0.1 yrs relevant vs a typical 1.0")).toBeTruthy();
    const text = fitText(container);
    expect(text).not.toMatch(/% match|64%\s*match/i);
    expect(text).not.toContain("Likely range");
    expect(container.querySelector(".breakdown-bar")).toBeNull();
  });

  it("impacts read as words with a bar sized against the largest one; the raw value stays in title and aria-label", async () => {
    mockApi(SCORED); // +0.39, -0.18, -0.04: max 0.39
    const { container } = renderWithProviders(<JobDetailView job={job} />);
    await screen.findByText("Fit score 64 · Partial match");
    const impacts = Array.from(container.querySelectorAll(".fit-impact"));
    expect(impacts.map((el) => el.textContent)).toEqual(["Big plus", "Gap", "Small gap"]);
    expect(impacts.map((el) => el.getAttribute("title"))).toEqual(["+0.39", "-0.18", "-0.04"]);
    expect(impacts[0].getAttribute("aria-label")).toBe("Big plus (+0.39)");
    const bars = impacts.map((el) => (el.querySelector(".fit-impact-fill") as HTMLElement).style.width);
    expect(bars).toEqual(["100%", "46%", "10%"]);
    expect(fitText(container)).not.toMatch(/[+-]0\.\d\d/);
  });

  it("a displayed interval replaces the number everywhere (D-54)", async () => {
    mockApi(SCORED_RANGE);
    const { container } = renderWithProviders(<JobDetailView job={job} />);
    expect(await screen.findByText("Fit score 60–68 · Partial match")).toBeTruthy();
    expect(container.querySelector(".match-number-large")?.textContent).toBe("60–68");
    const text = fitText(container);
    expect(text).not.toContain("64");
    expect(text).not.toContain("Likely range");
  });

  it("SCORED_LOW_CONFIDENCE (real engine output): the band only, never a number", async () => {
    mockApi(REAL.SCORED_LOW_CONFIDENCE);
    const { container } = renderWithProviders(
      <JobDetailView job={{ ...job, match_score: 0, match_band: "Limited match", match_outcome: "SCORED_LOW_CONFIDENCE" }} />,
    );
    expect(await screen.findByText(REAL.SCORED_LOW_CONFIDENCE.notes[0])).toBeTruthy();
    expect(screen.getAllByText("Limited match").length).toBeGreaterThan(0);
    expect(container.querySelector(".match-circle-large")).toBeNull();
    expect(container.querySelector(".fit-impact")).toBeNull();
  });

  it("hides impacts on a non-SCORED result even if the API sent them", async () => {
    const withImpacts = {
      ...REAL.SCORED_LOW_CONFIDENCE,
      strength_items: [{ label: "2 of 2 required skills", impact: "+0.39", note: "", code: "REQUIRED_SKILLS" }],
    };
    mockApi(withImpacts);
    const { container } = renderWithProviders(
      <JobDetailView job={{ ...job, match_score: 0, match_band: "Limited match", match_outcome: "SCORED_LOW_CONFIDENCE" }} />,
    );
    await screen.findByText("2 of 2 required skills");
    expect(container.querySelector(".fit-impact")).toBeNull();
    expect(fitText(container)).not.toContain("0.39");
  });

  it("NEEDS_REVIEW (real engine output): We couldn't score this, then the reason sentence", async () => {
    mockApi(REAL.NEEDS_REVIEW);
    const { container } = renderWithProviders(
      <JobDetailView job={{ ...job, match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" }} />,
    );
    expect(await screen.findByText("We couldn't find any requirements in this job posting.")).toBeTruthy();
    expect(screen.getAllByText("We couldn't score this").length).toBeGreaterThan(0);
    const text = fitText(container);
    expect(text).not.toMatch(/\d/);
    expect(text).not.toMatch(/error|fail|warning|!/i);
  });

  it("GATE_FAIL (real engine output): Not eligible with the reason, and a way to correct the facts", async () => {
    mockApi(REAL.GATE_FAIL);
    const { container } = renderWithProviders(
      <JobDetailView job={{ ...job, match_score: 0, match_band: null, match_outcome: "GATE_FAIL" }} />,
    );
    expect(await screen.findByText(REAL.GATE_FAIL.headline)).toBeTruthy();
    expect(screen.getByText(/If they've changed, update them/)).toBeTruthy();
    const link = screen.getByRole("link", { name: "Update your application answers" });
    expect(link.getAttribute("href")).toBe("/app/profile");
    expect(fitText(container)).not.toMatch(/\d/);
  });

  it("offers the profile link only for GATE_FAIL", async () => {
    mockApi(SCORED);
    renderWithProviders(<JobDetailView job={job} />);
    await screen.findByText("Fit score 64 · Partial match");
    expect(screen.queryByRole("link", { name: "Update your application answers" })).toBeNull();
  });

  it("ignores a breakdown without an outcome", async () => {
    mockApi({});
    const { container } = renderWithProviders(<JobDetailView job={job} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/41"));
    expect(fitText(container)).toContain("Partial match");
  });

  it("says to upload a résumé on a 400 and that scoring is unavailable on a 503", async () => {
    for (const [status, copy] of [
      [400, "Upload a résumé to see how you fit this job."],
      [503, "Match scoring is temporarily unavailable. Please try again later."],
    ] as const) {
      apiPost.mockImplementation((url: string) => {
        if (url.includes("/ai/match-breakdown/")) return Promise.reject({ response: { status } });
        if (url.endsWith("/check-live")) return new Promise(() => {});
        return Promise.resolve({ data: { sections: [], skills: [] } });
      });
      const { unmount } = renderWithProviders(<JobDetailView job={{ ...job, id: 60 + status }} />);
      expect(await screen.findByText(copy)).toBeTruthy();
      unmount();
    }
  });

  it("reports the new result to the page (onFitChange), even after the user moved on", async () => {
    mockApi(SCORED);
    const onFitChange = vi.fn();
    renderWithProviders(
      <JobDetailView job={{ ...job, match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" }} onFitChange={onFitChange} />,
    );
    await waitFor(() =>
      expect(onFitChange).toHaveBeenCalledWith(41, {
        match_outcome: "SCORED",
        match_band: "Partial match",
        match_score: 64,
      }),
    );
  });
});

describe("JobDetailView: details failure and job-switch race", () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    apiGet.mockResolvedValue({ data: [] });
    resetLiveChecks();
  });

  it("still runs and renders the breakdown when fetch-details fails", async () => {
    apiPost.mockImplementation((url: string) => {
      if (url.endsWith("/fetch-details")) return Promise.reject({ response: { status: 500 } });
      if (url.includes("/ai/match-breakdown/")) return Promise.resolve({ data: SCORED });
      if (url.endsWith("/check-live")) return new Promise(() => {});
      return Promise.resolve({ data: { sections: [], skills: [] } });
    });
    renderWithProviders(<JobDetailView job={{ ...job, id: 44, description: "" }} />);
    expect(await screen.findByText("Fit score 64 · Partial match")).toBeTruthy();
    const urls = apiPost.mock.calls.map((c) => String(c[0]));
    expect(urls.indexOf("/jobs/44/fetch-details")).toBeLessThan(urls.indexOf("/ai/match-breakdown/44"));
  });

  it("a late answer for job A patches only A and never reaches B's view", async () => {
    let resolveA: (v: unknown) => void = () => {};
    apiPost.mockImplementation((url: string) => {
      if (url === "/ai/match-breakdown/81") return new Promise((r) => { resolveA = r; });
      if (url === "/ai/match-breakdown/82") return Promise.resolve({ data: REAL.NEEDS_REVIEW });
      if (url.endsWith("/fetch-details")) return Promise.resolve({ data: { description: DESCRIPTION } });
      if (url.endsWith("/check-live")) return new Promise(() => {});
      return Promise.resolve({ data: { sections: [], skills: [] } });
    });
    const onFitChange = vi.fn();
    const jobA = { ...job, id: 81, title: "Job A", match_score: 0, match_band: null, match_outcome: null };
    const jobB = { ...job, id: 82, title: "Job B", match_score: 0, match_band: null, match_outcome: null };
    const { container, rerender } = renderWithProviders(<JobDetailView job={jobA} onFitChange={onFitChange} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/81"));
    rerender(
      <MemoryRouter>
        <ApplyTrackingProvider>
          <JobDetailView job={jobB} onFitChange={onFitChange} />
        </ApplyTrackingProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByText("We couldn't find any requirements in this job posting.")).toBeTruthy();
    resolveA({ data: SCORED });
    await waitFor(() =>
      expect(onFitChange).toHaveBeenCalledWith(81, { match_outcome: "SCORED", match_band: "Partial match", match_score: 64 }),
    );
    // B's own result only, and no patch for B carrying A's result.
    expect(onFitChange.mock.calls.filter(([id]) => id === 82)).toEqual([
      [82, { match_outcome: "NEEDS_REVIEW", match_band: null, match_score: 0 }],
    ]);
    expect(container.querySelector(".job-detail-title")?.textContent).toBe("Job B");
    const text = fitText(container);
    expect(text).toContain("We couldn't score this");
    expect(text).not.toMatch(/Fit score|64|Partial match/);
  });
});

describe("Jobs page card", () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    resetLiveChecks();
    window.history.replaceState({}, "", "/app");
  });

  const base = { ...job, saved: false, match_label: "" };

  function mockFeed(jobs: unknown[]) {
    apiGet.mockImplementation((url: string) => {
      if (url === "/jobs") return Promise.resolve({ data: jobs });
      if (url === "/jobs/stats") {
        return Promise.resolve({ data: { total: jobs.length, applied: 0, new: 0, avg_match_score: null, saved_count: 0 } });
      }
      return Promise.resolve({ data: [] });
    });
  }

  it(
    "shows the engine's result on each card, never a number, and a null average is fine",
    async () => {
      apiPost.mockImplementation(() => new Promise(() => {}));
      mockFeed([
        { ...base, id: 51, title: "Role Scored", match_score: 82, match_band: "Good match", match_outcome: "SCORED" },
        { ...base, id: 52, title: "Role Low", match_score: 0, match_band: "Partial match", match_outcome: "SCORED_LOW_CONFIDENCE" },
        { ...base, id: 53, title: "Role Gate", match_score: 0, match_band: null, match_outcome: "GATE_FAIL" },
        { ...base, id: 54, title: "Role Review", match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" },
        { ...base, id: 55, title: "Role New", match_score: 0, match_band: null, match_outcome: null },
      ]);
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      await waitFor(() => expect(container.querySelectorAll(".jobs-feed .job-card")).toHaveLength(5));
      const cards = Array.from(container.querySelectorAll(".jobs-feed .job-card"));
      const badge = (i: number) => cards[i].querySelector(".fit-badge")?.textContent ?? "";
      // The list payload carries no interval, so a SCORED card shows its band only (D-54).
      expect(badge(0)).toBe("Good match");
      expect(badge(1)).toBe("Partial match");
      expect(badge(2)).toBe("Not eligibleSee why");
      expect(badge(3)).toBe("We couldn't score this");
      expect(cards[4].querySelector(".fit-badge")).toBeNull();
      for (const card of cards) {
        expect(card.querySelector(".fit-badge")?.textContent ?? "").not.toMatch(/\d/);
        expect(card.textContent).not.toMatch(/\d%|STRONG MATCH|GOOD MATCH|FAIR MATCH/);
      }
    },
    20_000,
  );

  it(
    "updates the card when the open job's breakdown comes back",
    async () => {
      mockApi(SCORED);
      mockFeed([{ ...base, id: 71, title: "Was Unscorable", match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" }]);
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      const title = await screen.findByText("Was Unscorable", { selector: ".job-title" });
      const card = () => container.querySelector(".jobs-feed .job-card") as HTMLElement;
      expect(card().querySelector(".fit-badge")?.textContent).toBe("We couldn't score this");
      fireEvent.click(title);
      await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/71"));
      await waitFor(() => expect(card().querySelector(".fit-badge")?.textContent).toBe("Partial match"));
    },
    20_000,
  );

  it(
    "switching jobs before A's breakdown answers patches A's card only and leaves B's panel alone",
    async () => {
      let resolveA: (v: unknown) => void = () => {};
      apiPost.mockImplementation((url: string) => {
        if (url === "/ai/match-breakdown/91") return new Promise((r) => { resolveA = r; });
        if (url === "/ai/match-breakdown/92") return Promise.resolve({ data: REAL.NEEDS_REVIEW });
        if (url.endsWith("/fetch-details")) return Promise.resolve({ data: { description: DESCRIPTION } });
        if (url.endsWith("/check-live")) return new Promise(() => {});
        return Promise.resolve({ data: { sections: [], skills: [] } });
      });
      mockFeed([
        { ...base, id: 91, title: "Job A", match_score: 0, match_band: null, match_outcome: null },
        { ...base, id: 92, title: "Job B", match_score: 0, match_band: "Limited match", match_outcome: "SCORED_LOW_CONFIDENCE" },
      ]);
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      fireEvent.click(await screen.findByText("Job A", { selector: ".job-title" }));
      await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/ai/match-breakdown/91"));
      fireEvent.click(screen.getByText("Job B", { selector: ".job-title" }));
      await waitFor(() => expect(container.querySelector(".job-detail-title")?.textContent).toBe("Job B"));
      await screen.findByText("We couldn't find any requirements in this job posting.");
      resolveA({ data: SCORED });
      const cards = () => Array.from(container.querySelectorAll(".jobs-feed .job-card"));
      await waitFor(() => expect(cards()[0].querySelector(".fit-badge")?.textContent).toBe("Partial match"));
      // B's card took B's own breakdown; A's answer never touched it or B's panel.
      expect(cards()[1].querySelector(".fit-badge")?.textContent).toBe("We couldn't score this");
      expect(container.querySelector(".job-detail-title")?.textContent).toBe("Job B");
      expect(fitText(container)).not.toMatch(/Fit score|Partial match/);
    },
    20_000,
  );
});
