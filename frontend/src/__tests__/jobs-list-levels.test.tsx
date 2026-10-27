import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { render, waitFor } from "@testing-library/react";
import JobsList from "../pages/JobsList";

/**
 * /list is the public new-grad and internship list. The catalogue holds every level, so
 * the totals above the table must count the levels the table shows, not the catalogue.
 */
describe("JobsList levels", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch() {
    const fetchMock = vi.fn(async (url: string) => ({
      ok: true,
      json: async () => (url.startsWith("/jobs/stats") ? { total: 1234, new: 56 } : []),
    }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("asks for the totals and for the jobs at the same levels", async () => {
    const fetchMock = stubFetch();
    render(
      <MemoryRouter>
        <JobsList />
      </MemoryRouter>,
    );

    const urls = () => fetchMock.mock.calls.map((call) => String(call[0]));
    await waitFor(() => expect(urls().some((u) => u.startsWith("/jobs/stats"))).toBe(true));
    await waitFor(() => expect(urls().some((u) => u.startsWith("/jobs?"))).toBe(true));

    const stats = urls().find((u) => u.startsWith("/jobs/stats"));
    const list = urls().find((u) => u.startsWith("/jobs?"));
    expect(stats).toBe("/jobs/stats?experience_level=intern_new_grad%2Centry");
    expect(new URLSearchParams(list!.split("?")[1]).get("experience_level")).toBe("intern_new_grad,entry");
  });
});
