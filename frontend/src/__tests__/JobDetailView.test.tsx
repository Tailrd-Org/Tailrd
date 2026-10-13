import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import JobDetailView from "../components/JobDetailView";
import type { ReactElement } from "react";
import { ApplyTrackingProvider } from "../context/ApplyTracking";

const apiPost = vi.fn();
const apiGet = vi.fn();
vi.mock("../auth/api", () => ({
  default: {
    post: (...args: unknown[]) => apiPost(...args),
    get: (...args: unknown[]) => apiGet(...args),
  },
}));

// Real skillCovered; canned resume text so coverage highlighting is testable.
vi.mock("../lib/resumeCoverage", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/resumeCoverage")>();
  return {
    ...actual,
    getPrimaryResumeText: () => Promise.resolve("Shipped Python services and SQL pipelines."),
  };
});

function renderWithProviders(ui: ReactElement) {
  return render(<ApplyTrackingProvider>{ui}</ApplyTrackingProvider>);
}

const STRUCT = {
  sections: [
    { title: "Responsibilities", icon: "clipboard-list", items: ["Build planning models"] },
    {
      title: "Qualifications",
      icon: "graduation-cap",
      subsections: [
        { title: "Required", items: ["3-5 years of experience"] },
        { title: "Preferred", items: ["Project management experience"] },
      ],
    },
  ],
  skills: ["Python", "Hyperion Planning"],
  experience_years: "3-5",
  education: "",
};

const mockJob = {
  id: 1,
  title: "Senior Software Engineer",
  company: "TechCorp",
  location: "San Francisco, CA",
  url: "https://example.com/job/1",
  description:
    "We are looking for an engineer to build planning models and support EPM applications for finance teams.",
  match_score: 85,
  match_band: "Strong match",
  match_outcome: "SCORED",
  match_label: "Strong match",
  experience_score: 90,
  skill_score: 80,
  industry_score: 85,
  applicant_count: 42,
  source_platform: "linkedin",
  scraped_at: new Date(Date.now() - 3600000).toISOString(),
  salary_range: "$150k-$200k",
  status: "new",
};

describe("JobDetailView", () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    apiPost.mockImplementation((url: string) => {
      if (url.endsWith("/structure-description")) return Promise.resolve({ data: STRUCT });
      if (url.endsWith("/fetch-details")) {
        return Promise.resolve({ data: { description: mockJob.description } });
      }
      return Promise.resolve({ data: {} });
    });
    apiGet.mockResolvedValue({ data: [] });
  });

  it("renders job header with match score", () => {
    renderWithProviders(<JobDetailView job={mockJob} />);
    expect(screen.getByText("Senior Software Engineer")).toBeInTheDocument();
    expect(screen.getByText("TechCorp")).toBeInTheDocument();
    expect(screen.getByText("San Francisco, CA")).toBeInTheDocument();
    // The card's engine result shows until the breakdown answers; never "85%".
    expect(screen.getByText("Fit score 85 · Strong match")).toBeInTheDocument();
    expect(screen.queryByText(/85%/)).toBeNull();
  });

  it("upgrades to the server-side structured sections", async () => {
    renderWithProviders(<JobDetailView job={mockJob} />);
    await waitFor(() => {
      expect(screen.getByText("Responsibilities")).toBeInTheDocument();
    });
    expect(screen.getByText("Build planning models")).toBeInTheDocument();
    expect(screen.getByText("Required")).toBeInTheDocument();
    expect(screen.getByText("Preferred")).toBeInTheDocument();
    expect(screen.getByText("3-5 years of experience")).toBeInTheDocument();
  });

  it("highlights skill tags covered by the resume and leaves the rest neutral", async () => {
    renderWithProviders(<JobDetailView job={mockJob} />);
    await waitFor(() => {
      expect(screen.getByText("Python")).toBeInTheDocument();
    });
    expect(screen.getByText("Python").className).toContain("skill-tag-matched");
    expect(screen.getByText("Hyperion Planning").className).not.toContain("skill-tag-matched");
  });

  // Every open asks /ai/match-breakdown. The old copy told users to "Connect
  // Gemini or Ollama" on a 503, which no user can do.
  it("says match scoring is temporarily unavailable on a 503", async () => {
    apiPost.mockImplementation((url: string) => {
      if (url.includes("/ai/match-breakdown/")) {
        return Promise.reject({ response: { status: 503 } });
      }
      if (url.endsWith("/structure-description")) return Promise.resolve({ data: STRUCT });
      return Promise.resolve({ data: {} });
    });
    renderWithProviders(<JobDetailView job={{ ...mockJob, id: 5, match_score: 0 }} />);

    expect(
      await screen.findByText("Match scoring is temporarily unavailable. Please try again later.")
    ).toBeInTheDocument();
    expect(screen.queryByText(/gemini|ollama/i)).toBeNull();
  });

  it("shows a View Original Post CTA when no description could be fetched", async () => {
    apiPost.mockImplementation((url: string) => {
      if (url.endsWith("/fetch-details")) return Promise.resolve({ data: { description: "" } });
      return Promise.resolve({ data: { sections: [], skills: [] } });
    });
    const emptyJob = { ...mockJob, id: 3, description: "" };
    renderWithProviders(<JobDetailView job={emptyJob} />);
    await waitFor(() => {
      expect(screen.getByText("No description available")).toBeInTheDocument();
    });
    // Header action + the empty-state CTA both link out.
    expect(screen.getAllByText("View Original Post").length).toBeGreaterThanOrEqual(2);
  });

  it("uses the locations_json display when present", () => {
    const multiJob = {
      ...mockJob,
      id: 4,
      location: "Ottawa,Ontario,Canada; Kraków,Kraków,Poland; Łódź,Łódź,Poland",
      locations_json: [
        { city: "Ottawa", region: "ON", region_name: "Ontario", country: "Canada" },
        { city: "Kraków", region: "", region_name: "", country: "Poland" },
        { city: "Łódź", region: "", region_name: "", country: "Poland" },
      ],
    };
    renderWithProviders(<JobDetailView job={multiJob} />);
    expect(screen.getByText(/Ottawa, ON, Canada · \+2 more/)).toBeInTheDocument();
  });
});
