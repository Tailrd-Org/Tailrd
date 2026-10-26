import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { ApplyTrackingProvider } from "../context/ApplyTracking";
import { useAvailableExperienceLevels } from "../hooks/useAvailableExperienceLevels";

const apiGet = vi.fn();
vi.mock("../auth/api", () => ({
  default: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

// The bar reports what the page hands it, so the tests can read it.
vi.mock("../components/JobFilterBar", () => ({
  default: (props: { filters: { experience_level: string[] }; availableExperienceLevels?: string[] | null }) => (
    <div
      data-testid="job-filter-bar"
      data-levels={props.filters.experience_level.join(",")}
      data-available={props.availableExperienceLevels ? props.availableExperienceLevels.join(",") : "unknown"}
    />
  ),
}));

const FILTER_KEY = "job-aggregator-filters";

function renderWithProviders(ui: ReactElement) {
  return render(
    <MemoryRouter>
      <ApplyTrackingProvider>{ui}</ApplyTrackingProvider>
    </MemoryRouter>
  );
}

/** What the Jobs page's endpoints answer; `levels` is the body of GET /jobs/levels, or an error. */
function mockApi(levels: unknown) {
  apiGet.mockImplementation((url: string) => {
    if (url === "/jobs/levels") {
      return levels instanceof Error ? Promise.reject(levels) : Promise.resolve({ data: levels });
    }
    if (url === "/jobs/stats") {
      return Promise.resolve({ data: { total: 0, applied: 0, new: 0, avg_match_score: null, saved_count: 0 } });
    }
    // The feed and the rail: empty. (No "feed" key, so the rail ignores the body.)
    return Promise.resolve({ data: url === "/jobs" ? [] : {} });
  });
}

/** The params of the latest request to a path. */
function lastParams(path: string): any {
  const calls = apiGet.mock.calls.filter((call) => call[0] === path);
  return calls[calls.length - 1]?.[1]?.params;
}

/** The levels the feed was last asked for. */
function feedLevels(): string | null | undefined {
  return lastParams("/jobs")?.get("experience_level");
}

async function renderJobs() {
  const { default: Jobs } = await import("../pages/Jobs");
  return renderWithProviders(<Jobs />);
}

describe("Jobs page experience levels", () => {
  beforeEach(() => {
    apiGet.mockReset();
    localStorage.clear();
    window.history.replaceState({}, "", "/app");
  });

  it(
    "asks the feed, the stats and the rail for the levels that have jobs only",
    async () => {
      mockApi({ levels: ["intern_new_grad"] });
      await renderJobs();

      // Nothing saved means Intern/New Grad plus Entry Level, and Entry Level has no jobs.
      await waitFor(() => expect(feedLevels()).toBe("intern_new_grad"));
      await waitFor(() => expect(lastParams("/jobs/stats")).toEqual({ experience_level: "intern_new_grad" }));
      await waitFor(() => expect(lastParams("/jobs/sidebar")?.experience_level).toBe("intern_new_grad"));

      const bar = screen.getByTestId("job-filter-bar");
      expect(bar.dataset.levels).toBe("intern_new_grad");
      // The bar is told which options exist, to offer only those.
      expect(bar.dataset.available).toBe("intern_new_grad");
    },
    20_000,
  );

  it(
    "drops a saved level that has no jobs instead of filtering by something nobody can clear",
    async () => {
      localStorage.setItem(FILTER_KEY, JSON.stringify({ experience_level: ["senior"] }));
      mockApi({ levels: ["intern_new_grad"] });
      await renderJobs();

      await waitFor(() => expect(feedLevels()).toBe("intern_new_grad"));
      expect(lastParams("/jobs/stats")).toEqual({ experience_level: "intern_new_grad" });
    },
    20_000,
  );

  it(
    "keeps a saved level once its option has jobs",
    async () => {
      localStorage.setItem(FILTER_KEY, JSON.stringify({ experience_level: ["senior", "intern_new_grad"] }));
      mockApi({ levels: ["intern_new_grad", "senior"] });
      await renderJobs();

      await waitFor(() => expect(screen.getByTestId("job-filter-bar").dataset.available).toBe("intern_new_grad,senior"));
      await waitFor(() => expect(feedLevels()).toBe("senior,intern_new_grad"));
    },
    20_000,
  );

  it(
    "uses the saved or default levels untouched while the options are not known",
    async () => {
      mockApi(new Error("Network Error"));
      await renderJobs();

      await waitFor(() => expect(feedLevels()).toBe("intern_new_grad,entry"));
      await act(async () => {});
      expect(apiGet).toHaveBeenCalledWith("/jobs/levels");
      expect(screen.getByTestId("job-filter-bar").dataset.available).toBe("unknown");
      expect(feedLevels()).toBe("intern_new_grad,entry");
      expect(lastParams("/jobs/stats")).toEqual({ experience_level: "intern_new_grad,entry" });
    },
    20_000,
  );

  it(
    "keeps the rail's last-visit window next to the levels",
    async () => {
      localStorage.setItem("tailrd.jobs.lastVisit", "2026-10-20T12:00:00.000Z");
      mockApi({ levels: ["intern_new_grad"] });
      await renderJobs();

      await waitFor(() =>
        expect(lastParams("/jobs/sidebar")).toEqual({
          since: "2026-10-20T12:00:00.000Z",
          experience_level: "intern_new_grad",
        }),
      );
    },
    20_000,
  );
});

describe("useAvailableExperienceLevels", () => {
  beforeEach(() => {
    apiGet.mockReset();
  });

  it("is null until the answer arrives, then the options that have jobs", async () => {
    mockApi({ levels: ["intern_new_grad", "mid"] });
    const { result } = renderHook(() => useAvailableExperienceLevels());
    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current).toEqual(["intern_new_grad", "mid"]));
    expect(apiGet).toHaveBeenCalledWith("/jobs/levels");
  });

  it("stays null when the request fails", async () => {
    mockApi(new Error("503"));
    const { result } = renderHook(() => useAvailableExperienceLevels());
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
    await act(async () => {});
    expect(result.current).toBeNull();
  });

  it("stays null when the answer is not usable", async () => {
    for (const body of [[], "<html>", { levels: [] }, { levels: ["astronaut"] }, {}]) {
      apiGet.mockReset();
      mockApi(body);
      const { result, unmount } = renderHook(() => useAvailableExperienceLevels());
      await waitFor(() => expect(apiGet).toHaveBeenCalled());
      await act(async () => {});
      expect(result.current).toBeNull();
      unmount();
    }
  });
});
