import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

vi.mock("../components/JobFilterBar", () => ({
  default: () => <div data-testid="job-filter-bar" />,
  normalizeExperienceLevels: (v: unknown) => (Array.isArray(v) ? v : []),
}));

function renderWithProviders(ui: ReactElement) {
  return render(
    <MemoryRouter>
      <ApplyTrackingProvider>{ui}</ApplyTrackingProvider>
    </MemoryRouter>
  );
}

const baseJob = {
  id: 11,
  title: "Software Engineer, New Grad",
  company: "Acme",
  location: "Toronto, ON",
  url: "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Toronto/SWE_R123",
  description:
    "Build and ship services used by millions of people. Work with a small team on distributed systems.",
  match_score: 0,
  match_label: "",
  experience_score: 0,
  skill_score: 0,
  industry_score: 0,
  applicant_count: null,
  source_platform: "ats",
  ats_type: "workday",
  scraped_at: new Date(Date.now() - 3_600_000).toISOString(),
  salary_range: "",
  status: "new",
  listing_status: "active",
};

type LiveAnswer = { id: number; listing_status: string; verdict: string } | Error | "pending";

function mockApi(liveAnswer: LiveAnswer) {
  apiPost.mockImplementation((url: string) => {
    if (url.endsWith("/check-live")) {
      if (liveAnswer === "pending") return new Promise(() => {});
      if (liveAnswer instanceof Error) return Promise.reject(liveAnswer);
      return Promise.resolve({ data: liveAnswer });
    }
    if (url.endsWith("/structure-description")) return Promise.resolve({ data: { sections: [], skills: [] } });
    return Promise.resolve({ data: {} });
  });
}

function notFound() {
  return Object.assign(new Error("HTTP 404"), { response: { status: 404, data: { detail: "Not Found" } } });
}

describe("JobDetailView closed state", () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    apiGet.mockResolvedValue({ data: [] });
    resetLiveChecks();
  });

  it("shows the closed badge and never links a removed listing", () => {
    mockApi("pending");
    renderWithProviders(<JobDetailView job={{ ...baseJob, listing_status: "removed" }} />);
    expect(screen.getByText("No longer accepting applications")).toBeInTheDocument();
    const apply = screen.getByRole("button", { name: /Apply with Tailrd/i });
    expect(apply).toBeDisabled();
    expect(screen.getByRole("button", { name: /View Original Post/i })).toBeDisabled();
    expect(screen.queryByRole("link", { name: /Apply with Tailrd/i })).toBeNull();
  });

  it("treats an expired listing as closed too", () => {
    mockApi("pending");
    renderWithProviders(<JobDetailView job={{ ...baseJob, listing_status: "expired" }} />);
    expect(screen.getByText("No longer accepting applications")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Apply with Tailrd/i })).toBeDisabled();
  });

  it("keeps Apply usable while the live check is in flight", () => {
    mockApi("pending");
    renderWithProviders(<JobDetailView job={baseJob} />);
    expect(apiPost).toHaveBeenCalledWith("/jobs/11/check-live");
    const apply = screen.getByRole("link", { name: /Apply with Tailrd/i });
    expect(apply).toHaveAttribute("href", baseJob.url);
    expect(screen.queryByText("No longer accepting applications")).toBeNull();
  });

  it("switches to the closed state when the live check says dead", async () => {
    mockApi({ id: 11, listing_status: "removed", verdict: "dead" });
    const onChange = vi.fn();
    renderWithProviders(<JobDetailView job={baseJob} onListingStatusChange={onChange} />);
    await waitFor(() => {
      expect(screen.getByText("No longer accepting applications")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /Apply with Tailrd/i })).toBeDisabled();
    expect(onChange).toHaveBeenCalledWith(11, "removed");
  });

  it("closes on a dead verdict even if the status came back unchanged", async () => {
    mockApi({ id: 11, listing_status: "active", verdict: "dead" });
    const onChange = vi.fn();
    renderWithProviders(<JobDetailView job={baseJob} onListingStatusChange={onChange} />);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(11, "removed"));
    expect(screen.getByText("No longer accepting applications")).toBeInTheDocument();
  });

  it("stays open and quiet when the endpoint is missing", async () => {
    mockApi(notFound());
    const onChange = vi.fn();
    renderWithProviders(<JobDetailView job={baseJob} onListingStatusChange={onChange} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole("link", { name: /Apply with Tailrd/i })).toBeInTheDocument();
    expect(screen.queryByText("No longer accepting applications")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not report an alive listing whose status is unchanged", async () => {
    mockApi({ id: 11, listing_status: "active", verdict: "alive" });
    const onChange = vi.fn();
    renderWithProviders(<JobDetailView job={baseJob} onListingStatusChange={onChange} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/jobs/11/check-live"));
    await act(async () => {
      await Promise.resolve();
    });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: /Apply with Tailrd/i })).toBeInTheDocument();
  });

  it("labels the source from source_platform, not always LinkedIn", () => {
    mockApi("pending");
    renderWithProviders(<JobDetailView job={baseJob} />);
    expect(screen.getByText("Workday")).toBeInTheDocument();
    expect(screen.queryByText("LinkedIn")).toBeNull();
  });

  it("ignores a late fetch-details answer for the job it has moved on from", async () => {
    let answerA: (value: unknown) => void = () => {};
    apiPost.mockImplementation((url: string) => {
      if (url === "/jobs/31/fetch-details") return new Promise((resolve) => (answerA = resolve));
      if (url.endsWith("/structure-description")) return Promise.resolve({ data: { sections: [], skills: [] } });
      return new Promise(() => {});
    });
    const onChange = vi.fn();
    const jobA = { ...baseJob, id: 31, title: "Role A", description: "", url: "https://a.example/job/31" };
    const jobB = { ...baseJob, id: 32, title: "Role B", url: "https://b.example/job/32" };
    const { rerender } = renderWithProviders(<JobDetailView job={jobA} onListingStatusChange={onChange} />);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/jobs/31/fetch-details"));
    expect(screen.getByText("Loading job details...")).toBeInTheDocument();

    rerender(
      <ApplyTrackingProvider>
        <JobDetailView job={jobB} onListingStatusChange={onChange} />
      </ApplyTrackingProvider>,
    );
    // B already has its text: A's pending fetch must not keep a spinner up.
    expect(screen.queryByText("Loading job details...")).toBeNull();
    await act(async () => {
      answerA({
        data: {
          dead: true,
          listing_status: "removed",
          description: "Role A posting text that must never show on Role B.",
          apply_url: "https://a.example/apply/31",
          company_logo: "",
        },
      });
    });

    // B is still open, with its own link and text.
    expect(screen.queryByText("No longer accepting applications")).toBeNull();
    expect(screen.getByRole("link", { name: /Apply with Tailrd/i })).toHaveAttribute("href", jobB.url);
    expect(screen.queryByText(/Role A posting text/)).toBeNull();
    expect(screen.queryByText("Loading job details...")).toBeNull();
    // A is still reported closed, so the feed drops it.
    expect(onChange).toHaveBeenCalledWith(31, "removed");
  });
});

describe("Jobs page", () => {
  const feedJob = { ...baseJob, id: 21, company: "**Tesla**", saved: false };

  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    resetLiveChecks();
    window.history.replaceState({}, "", "/app");
  });

  function mockFeed(jobs: unknown[], deepLinked?: unknown) {
    apiGet.mockImplementation((url: string) => {
      if (url === "/jobs") return Promise.resolve({ data: jobs });
      if (url === "/jobs/stats") {
        return Promise.resolve({ data: { total: 0, applied: 0, new: 0, avg_match_score: 0, saved_count: 0 } });
      }
      if (deepLinked && url.startsWith("/jobs/")) return Promise.resolve({ data: deepLinked });
      return Promise.resolve({ data: [] });
    });
  }

  it(
    "keeps a deep-linked job open when it is not on the current page",
    async () => {
      mockApi("pending");
      const linked = { ...baseJob, id: 99, title: "Deep Linked Role", company: "**Beta**", listing_status: "expired" };
      mockFeed([feedJob], linked);
      window.history.replaceState({}, "", "/app?job=99");
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);

      await waitFor(() => {
        expect(container.querySelector(".job-detail-title")?.textContent).toBe("Deep Linked Role");
      });
      // The feed loaded without it, and the panel is still open.
      await waitFor(() => expect(container.querySelectorAll(".jobs-feed .job-card")).toHaveLength(1));
      expect(container.querySelector(".job-detail-title")?.textContent).toBe("Deep Linked Role");
      expect(container.querySelector(".job-detail-company")?.textContent).toBe("Beta");
      // Removed/expired deep links render closed.
      expect(container.querySelector(".job-detail-view .listing-closed-badge")).not.toBeNull();
      expect(window.location.search).toBe("");
    },
    20_000,
  );

  it(
    "drops a job the live check finds dead from the feed but keeps its panel on the closed state",
    async () => {
      mockApi({ id: 21, listing_status: "removed", verdict: "dead" });
      const other = { ...baseJob, id: 22, title: "Other Role", company: "Other Co", saved: false };
      mockFeed([feedJob, other]);
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);

      const card = await screen.findByText("Software Engineer, New Grad", { selector: ".job-title" });
      // Markdown-bold names are cleaned in the feed.
      expect(container.querySelector(".job-company")?.textContent).toContain("Tesla");
      expect(container.querySelector(".job-company")?.textContent).not.toContain("*");

      act(() => {
        card.click();
      });

      await waitFor(() => {
        expect(container.querySelector(".job-detail-view .listing-closed-badge")).not.toBeNull();
      });
      await waitFor(() => {
        expect(container.querySelector(".jobs-feed .job-title")?.textContent).toBe("Other Role");
      });
      expect(container.querySelectorAll(".jobs-feed .job-card")).toHaveLength(1);
      expect(container.querySelector(".job-detail-title")?.textContent).toBe("Software Engineer, New Grad");
      expect(screen.getByRole("button", { name: /Apply with Tailrd/i })).toBeDisabled();
    },
    20_000,
  );

  it(
    "keeps the job on screen open when the previous job's fetch-details finds that one dead",
    async () => {
      const jobA = { ...baseJob, id: 31, title: "Role A", description: "", url: "https://a.example/job/31", saved: false };
      const jobB = { ...baseJob, id: 32, title: "Role B", url: "https://b.example/job/32", saved: false };
      mockFeed([jobA, jobB]);
      let answerA: (value: unknown) => void = () => {};
      apiPost.mockImplementation((url: string) => {
        if (url === "/jobs/31/fetch-details") return new Promise((resolve) => (answerA = resolve));
        if (url === "/jobs/32/check-live") {
          return Promise.resolve({ data: { id: 32, listing_status: "active", verdict: "alive" } });
        }
        if (url.endsWith("/structure-description")) return Promise.resolve({ data: { sections: [], skills: [] } });
        return new Promise(() => {});
      });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);

      const cardA = await screen.findByText("Role A", { selector: ".job-title" });
      act(() => cardA.click());
      await waitFor(() => expect(container.querySelector(".job-detail-title")?.textContent).toBe("Role A"));
      act(() => screen.getByText("Role B", { selector: ".job-title" }).click());
      await waitFor(() => expect(container.querySelector(".job-detail-title")?.textContent).toBe("Role B"));

      await act(async () => {
        answerA({ data: { id: 31, dead: true, listing_status: "removed", description: "", apply_url: jobA.url } });
      });

      expect(container.querySelector(".job-detail-title")?.textContent).toBe("Role B");
      expect(container.querySelector(".job-detail-view .listing-closed-badge")).toBeNull();
      expect(
        container.querySelector(".job-detail-view a.btn-apply-detail")?.getAttribute("href"),
      ).toBe(jobB.url);
      // A itself is dead: it leaves the feed.
      await waitFor(() =>
        expect(Array.from(container.querySelectorAll(".jobs-feed .job-title")).map((e) => e.textContent)).toEqual([
          "Role B",
        ]),
      );
    },
    20_000,
  );
});

describe("Jobs page: late verdicts and tabs", () => {
  const desc = "A long enough description so fetch-details short-circuits in the client code path.";
  const X = { ...baseJob, id: 41, title: "Saved X", url: "https://x.example/41", description: desc, saved: true };
  const O = { ...baseJob, id: 42, title: "Other O", url: "https://o.example/42", description: desc, saved: true };
  const Y = { ...baseJob, id: 43, title: "Plain Y", url: "https://y.example/43", description: desc, saved: false };

  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    resetLiveChecks();
    window.history.replaceState({}, "", "/app");
  });

  function mockTabs(all: { saved: boolean }[]) {
    apiGet.mockImplementation((url: string, config?: { params?: URLSearchParams }) => {
      if (url === "/jobs") {
        const liked = config?.params?.get("saved") === "1";
        return Promise.resolve({ data: liked ? all.filter((j) => j.saved) : all });
      }
      if (url === "/jobs/stats") {
        return Promise.resolve({ data: { total: 0, applied: 0, new: 0, avg_match_score: 0, saved_count: 2 } });
      }
      return Promise.resolve({ data: [] });
    });
  }

  function feedTitles(container: HTMLElement) {
    return Array.from(container.querySelectorAll(".jobs-feed .job-title")).map((e) => e.textContent);
  }

  function likedTab(container: HTMLElement) {
    return Array.from(container.querySelectorAll(".tab-btn")).find((b) =>
      (b.textContent || "").startsWith("Liked"),
    ) as HTMLButtonElement;
  }

  it(
    "keeps a saved job in the Liked list when its dead verdict lands after the tab switch",
    async () => {
      mockTabs([X, O, Y]);
      let answerX: (value: unknown) => void = () => {};
      apiPost.mockImplementation((url: string) => {
        if (url === "/jobs/41/check-live") return new Promise((resolve) => (answerX = resolve));
        if (url.endsWith("/structure-description")) return Promise.resolve({ data: { sections: [], skills: [] } });
        return new Promise(() => {});
      });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);

      const cardX = await screen.findByText("Saved X", { selector: ".job-title" });
      act(() => cardX.click());
      await waitFor(() => expect(container.querySelector(".job-detail-title")?.textContent).toBe("Saved X"));
      act(() => likedTab(container).click());
      await waitFor(() => expect(feedTitles(container)).toEqual(["Saved X", "Other O"]));

      await act(async () => {
        answerX({ data: { id: 41, listing_status: "removed", verdict: "dead" } });
      });

      // Liked keeps saved jobs and shows them closed.
      expect(feedTitles(container)).toEqual(["Saved X", "Other O"]);
      expect(container.querySelector(".jobs-feed .listing-closed-badge")).not.toBeNull();
      expect(container.querySelector(".job-detail-view .listing-closed-badge")).not.toBeNull();
    },
    20_000,
  );

  it(
    "does not let a late dead verdict for an earlier job close the panel on screen",
    async () => {
      mockTabs([X, O, Y]);
      let answerY: (value: unknown) => void = () => {};
      apiPost.mockImplementation((url: string) => {
        if (url === "/jobs/43/check-live") return new Promise((resolve) => (answerY = resolve));
        if (url === "/jobs/42/check-live") {
          return Promise.resolve({ data: { id: 42, listing_status: "removed", verdict: "dead" } });
        }
        if (url.endsWith("/structure-description")) return Promise.resolve({ data: { sections: [], skills: [] } });
        return new Promise(() => {});
      });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);

      const cardY = await screen.findByText("Plain Y", { selector: ".job-title" });
      act(() => cardY.click());
      await waitFor(() => expect(container.querySelector(".job-detail-title")?.textContent).toBe("Plain Y"));
      act(() => screen.getByText("Other O", { selector: ".job-title" }).click());
      // O is dead: it leaves the feed and its panel stays on the closed state.
      await waitFor(() => expect(container.querySelector(".job-detail-view .listing-closed-badge")).not.toBeNull());
      expect(container.querySelector(".job-detail-title")?.textContent).toBe("Other O");

      await act(async () => {
        answerY({ data: { id: 43, listing_status: "removed", verdict: "dead" } });
      });

      expect(container.querySelector(".job-detail-title")?.textContent).toBe("Other O");
      expect(feedTitles(container)).toEqual(["Saved X"]);
    },
    20_000,
  );

  it(
    "offers no Apply Now link to a closed saved job from the Cover Letter modal",
    async () => {
      const closedSaved = { ...X, id: 51, title: "Closed Saved", url: "https://dead.example/51", listing_status: "removed" };
      mockTabs([closedSaved]);
      apiPost.mockImplementation((url: string) => {
        if (url.startsWith("/ai/cover-letter/")) return Promise.resolve({ data: { text: "Dear team" } });
        return new Promise(() => {});
      });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);

      await screen.findByText("Closed Saved", { selector: ".job-title" });
      act(() => likedTab(container).click());
      await screen.findByText("Closed Saved", { selector: ".job-title" });
      expect(container.querySelector(".jobs-feed .btn-apply")).toBeDisabled();

      act(() => screen.getByRole("button", { name: /Cover Letter/i }).click());
      const closedButton = await screen.findByRole("button", { name: /Posting closed/i });
      expect(closedButton).toBeDisabled();
      expect(screen.queryByRole("link", { name: /Apply Now/i })).toBeNull();
      expect(document.querySelector("a[href='https://dead.example/51']")).toBeNull();
    },
    20_000,
  );
});

describe("Apply checks the listing before opening it", () => {
  const desc = "A long enough description so fetch-details short-circuits in the client code path.";
  const card = { ...baseJob, id: 61, title: "Card Role", url: "https://acme.example/job/61", description: desc, saved: false };
  const other = { ...baseJob, id: 62, title: "Other Role", url: "https://acme.example/job/62", description: desc, saved: false };

  let tab: { opener: unknown; closed: boolean; location: { href: string }; close: ReturnType<typeof vi.fn> };
  let openSpy: ReturnType<typeof vi.spyOn>;
  const sendMessage = vi.fn();

  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    resetLiveChecks();
    sendMessage.mockReset();
    window.history.replaceState({}, "", "/app");
    tab = { opener: window, closed: false, location: { href: "about:blank" }, close: vi.fn() };
    openSpy = vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    // registerApplyClick hands the apply to the extension through this.
    (window as unknown as { chrome?: unknown }).chrome = { runtime: { sendMessage } };
  });

  afterEach(() => {
    openSpy.mockRestore();
    delete (window as unknown as { chrome?: unknown }).chrome;
  });

  function mockJobs(jobs: unknown[]) {
    apiGet.mockImplementation((url: string) => {
      if (url === "/jobs") return Promise.resolve({ data: jobs });
      if (url === "/jobs/stats") {
        return Promise.resolve({ data: { total: 0, applied: 0, new: 0, avg_match_score: 0, saved_count: 0 } });
      }
      return Promise.resolve({ data: [] });
    });
  }

  function mockLive(answers: Record<number, unknown>) {
    apiPost.mockImplementation((url: string) => {
      const m = url.match(/^\/jobs\/(\d+)\/check-live$/);
      if (m) {
        const answer = answers[Number(m[1])];
        if (answer === undefined) return new Promise(() => {});
        if (answer instanceof Promise) return answer;
        return Promise.resolve({ data: answer });
      }
      if (url.endsWith("/structure-description")) return Promise.resolve({ data: { sections: [], skills: [] } });
      return new Promise(() => {});
    });
  }

  function applyIntents() {
    return sendMessage.mock.calls.map(([, message]) => message as { type: string; jobId: number; url: string });
  }

  function cardApply(container: HTMLElement, title: string) {
    const cardEl = Array.from(container.querySelectorAll(".jobs-feed .job-card")).find(
      (el) => el.querySelector(".job-title")?.textContent === title,
    ) as HTMLElement;
    return cardEl.querySelector(".btn-apply") as HTMLElement;
  }

  it(
    "closes the tab and flips the card to closed when the listing is dead",
    async () => {
      mockJobs([card, other]);
      mockLive({ 61: { id: 61, listing_status: "active", verdict: "dead" } });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      await screen.findByText("Card Role", { selector: ".job-title" });

      let followed = true;
      await act(async () => {
        followed = fireEvent.click(cardApply(container, "Card Role"));
      });

      expect(followed).toBe(false); // the stored link was not followed
      expect(openSpy).toHaveBeenCalledWith("about:blank", "_blank");
      await waitFor(() => expect(tab.close).toHaveBeenCalled());
      expect(tab.location.href).toBe("about:blank");
      // The card stays in place, shown closed, with Apply disabled.
      await waitFor(() => expect(cardApply(container, "Card Role").tagName).toBe("BUTTON"));
      expect(cardApply(container, "Card Role")).toBeDisabled();
      expect(container.querySelector(".jobs-feed .listing-closed-badge")).not.toBeNull();
      // No apply was recorded or handed to the extension.
      expect(applyIntents()).toEqual([]);
    },
    20_000,
  );

  it(
    "opens the job in the new tab and records the apply when the listing is alive",
    async () => {
      mockJobs([card]);
      mockLive({ 61: { id: 61, listing_status: "active", verdict: "alive" } });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      await screen.findByText("Card Role", { selector: ".job-title" });

      await act(async () => {
        fireEvent.click(cardApply(container, "Card Role"));
      });

      await waitFor(() => expect(tab.location.href).toBe(card.url));
      expect(tab.opener).toBeNull();
      expect(tab.close).not.toHaveBeenCalled();
      await waitFor(() =>
        expect(applyIntents()).toContainEqual(
          expect.objectContaining({ type: "TAILRD_APPLY_INTENT", jobId: 61, url: card.url }),
        ),
      );
      expect(container.querySelector(".jobs-feed .listing-closed-badge")).toBeNull();
      // The card was not selected by the Apply click.
      expect(container.querySelector(".job-detail-view")).toBeNull();
    },
    20_000,
  );

  it(
    "follows Indeed links straight away, since they cannot be probed",
    async () => {
      const indeed = { ...card, id: 63, title: "Indeed Role", url: "https://ca.indeed.com/viewjob?jk=abc" };
      mockJobs([indeed]);
      mockLive({});
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      await screen.findByText("Indeed Role", { selector: ".job-title" });

      const link = cardApply(container, "Indeed Role");
      link.addEventListener("click", (e) => e.preventDefault()); // keep jsdom from navigating
      act(() => {
        link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
      });

      expect(openSpy).not.toHaveBeenCalled();
      expect(apiPost).not.toHaveBeenCalledWith("/jobs/63/check-live");
      expect(applyIntents()).toContainEqual(expect.objectContaining({ jobId: 63, url: indeed.url }));
    },
    20_000,
  );

  it(
    "waits on the detail panel's pending check before opening its Apply link",
    async () => {
      mockJobs([card]);
      let answer: (value: unknown) => void = () => {};
      mockLive({ 61: new Promise((resolve) => (answer = resolve)) });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      const title = await screen.findByText("Card Role", { selector: ".job-title" });
      act(() => title.click());
      const apply = await waitFor(() => {
        const link = container.querySelector(".job-detail-view a.btn-apply-detail");
        if (!link) throw new Error("no panel Apply link yet");
        return link as HTMLElement;
      });

      let followed = true;
      act(() => {
        followed = fireEvent.click(apply);
      });
      expect(followed).toBe(false);
      expect(openSpy).toHaveBeenCalledWith("about:blank", "_blank");

      await act(async () => {
        answer({ data: { id: 61, listing_status: "removed", verdict: "dead" } });
      });

      await waitFor(() => expect(tab.close).toHaveBeenCalled());
      expect(tab.location.href).toBe("about:blank");
      expect(container.querySelector(".job-detail-view .listing-closed-badge")).not.toBeNull();
      expect(container.querySelector(".job-detail-view button.btn-apply-detail")).toBeDisabled();
      expect(applyIntents()).toEqual([]);
    },
    20_000,
  );

  it(
    "opens the detail panel's Apply link directly once the check has answered",
    async () => {
      mockJobs([card]);
      mockLive({ 61: { id: 61, listing_status: "active", verdict: "alive" } });
      const { default: Jobs } = await import("../pages/Jobs");
      const { container } = renderWithProviders(<Jobs />);
      const title = await screen.findByText("Card Role", { selector: ".job-title" });
      act(() => title.click());
      await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/jobs/61/check-live"));
      await act(async () => {
        await Promise.resolve();
      });

      const apply = container.querySelector(".job-detail-view a.btn-apply-detail") as HTMLElement;
      apply.addEventListener("click", (e) => e.preventDefault()); // keep jsdom from navigating
      act(() => {
        apply.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
      });

      expect(openSpy).not.toHaveBeenCalled();
      expect(applyIntents()).toContainEqual(expect.objectContaining({ jobId: 61, url: card.url }));
    },
    20_000,
  );
});
