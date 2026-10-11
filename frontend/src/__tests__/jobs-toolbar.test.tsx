import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ApplyTrackingProvider } from "../context/ApplyTracking";
import Jobs from "../pages/Jobs";

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPut = vi.fn();

vi.mock("../auth/api", () => ({
  default: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    put: (...args: unknown[]) => apiPut(...args),
  },
}));

vi.mock("../components/JobFilterBar", () => ({
  default: () => <div data-testid="job-filter-bar" />,
  normalizeExperienceLevels: (value: unknown) => (Array.isArray(value) ? value : []),
}));

vi.mock("../components/JobsSidebar", () => ({ default: () => null }));

function renderJobs() {
  return render(
    <MemoryRouter>
      <ApplyTrackingProvider>
        <Jobs />
      </ApplyTrackingProvider>
    </MemoryRouter>,
  );
}

describe("Jobs filter toolbar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiGet.mockImplementation((url: string) => {
      if (url === "/jobs/stats") {
        return Promise.resolve({ data: { total: 0, applied: 0, new: 0, avg_match_score: 0, saved_count: 0 } });
      }
      return Promise.resolve({ data: [] });
    });
  });

  afterEach(() => cleanup());

  it("keeps filters in the toolbar without a visibility toggle", () => {
    const { container } = renderJobs();
    const toolbar = container.querySelector(".jobs-toolbar");
    const filterBar = screen.getByTestId("job-filter-bar");

    expect(toolbar).toContainElement(filterBar);
    expect(screen.queryByRole("button", { name: /hide filters|show filters/i })).not.toBeInTheDocument();
    expect(toolbar?.children).toHaveLength(1);
    expect(toolbar?.firstElementChild).toContainElement(filterBar);
  });
});
