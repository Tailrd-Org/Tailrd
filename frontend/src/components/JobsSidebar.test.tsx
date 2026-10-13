import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import JobsSidebar, { type SidebarData } from "./JobsSidebar";

const ping = vi.fn();
vi.mock("../lib/extensionBridge", () => ({ pingExtension: () => ping() }));

const DATA: SidebarData = {
  resume: { id: 7, name: "Main CV", scored_jobs: 40, avg_match: 72, strong_matches: 5, gap_pool_size: 30 },
  resume_skills: ["python", "react"],
  skill_gaps: [
    { skill: "aws", job_count: 12 },
    { skill: "node.js", job_count: 4 },
  ],
  progress: { week_start: "2026-10-05T00:00:00Z", applied_week: 3, applied_total: 9, saved_week: 2, interviews: 1 },
  closing_soon: [
    { id: 31, title: "Backend Intern", company: "Acme", company_logo: "", company_domain: "", company_url: "", age_days: 4, reason: "stale" },
    { id: 32, title: "SWE New Grad", company: "Beta", company_logo: "", company_domain: "", company_url: "", age_days: 26, reason: "old" },
  ],
  feed: { total: 4373, new_since: 120, since: "2026-10-07T00:00:00Z", remote: 300, strong_matches: 5 },
  top_companies: [
    { company: "Shopify", count: 6, company_logo: "", company_domain: "", company_url: "" },
  ],
  top_companies_basis: "matches",
  autofill: { fields_filled: 1234, passes: 20 },
  alerts_enabled: true,
};

function setup(data: SidebarData | null = DATA, overrides: Partial<Parameters<typeof JobsSidebar>[0]> = {}) {
  const props = {
    data,
    onShowStrong: vi.fn(),
    onShowNew: vi.fn(),
    onShowRemote: vi.fn(),
    onCompany: vi.fn(),
    onOpenJob: vi.fn(),
    onAlertsChange: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(
    <MemoryRouter>
      <JobsSidebar {...props} />
    </MemoryRouter>,
  );
  return props;
}

describe("JobsSidebar", () => {
  beforeEach(() => {
    ping.mockReset();
    ping.mockResolvedValue("connected");
    localStorage.clear();
  });

  it("shows a skeleton while loading", () => {
    setup(null);
    expect(screen.getByLabelText("Your job search").getAttribute("aria-busy")).toBe("true");
  });

  it("shows the résumé snapshot with display-cased skill gaps and a review link", () => {
    setup();
    expect(screen.getByText("Main CV")).toBeTruthy();
    expect(screen.getByText("72 average fit score")).toBeTruthy();
    expect(screen.getByText("AWS")).toBeTruthy();
    expect(screen.getByText("Node.js")).toBeTruthy();
    expect(screen.getByText(/actually used/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /Review résumé/ }).getAttribute("href")).toBe("/app/resume/7");
  });

  it("names the average by the engine's bands and never shows a percent match", () => {
    setup();
    // 72 is a Good match under 85 / 70 / 55 (it was nothing special under the old 80 cut).
    expect(screen.getByText(/Good match/)).toBeTruthy();
    expect(screen.getByRole("img", { name: "Average fit score 72" })).toBeTruthy();
    const rail = screen.getByLabelText("Your job search").textContent ?? "";
    expect(rail).not.toMatch(/\d%|% match|80%/);
  });

  it("alert copy follows the Strong band (fit score 85 or higher)", () => {
    setup();
    expect(screen.getByText(/Strong match \(fit score 85 or higher\)/)).toBeTruthy();
    setup({ ...DATA, alerts_enabled: false });
    expect(screen.getAllByText(/Strong match \(fit score 85 or higher\)/).length).toBe(2);
  });

  it("asks for a résumé when there is none", () => {
    setup({ ...DATA, resume: null, skill_gaps: [] });
    expect(screen.getByRole("link", { name: /Upload résumé/ }).getAttribute("href")).toBe("/app/resume");
  });

  it("tracks progress against a weekly goal the user can change and that persists", () => {
    setup();
    expect(screen.getByText("3 of 10 applications")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Lower weekly goal"));
    expect(screen.getByText("3 of 9 applications")).toBeTruthy();
    expect(localStorage.getItem("tailrd.jobs.weeklyGoal")).toBe("9");
  });

  it("opens a closing-soon job and labels stale vs old", () => {
    const props = setup();
    expect(screen.getByText("may be closing")).toBeTruthy();
    expect(screen.getByText("posted 26d ago")).toBeTruthy();
    fireEvent.click(screen.getByText("SWE New Grad"));
    expect(props.onOpenJob).toHaveBeenCalledWith(32);
  });

  it("hides the closing-soon card when nothing is closing", () => {
    setup({ ...DATA, closing_soon: [] });
    expect(screen.queryByText("Apply before these close")).toBeNull();
  });

  it("feed summary numbers apply filters", () => {
    const props = setup();
    fireEvent.click(screen.getByText("new since last visit"));
    expect(props.onShowNew).toHaveBeenCalledWith("2026-10-07T00:00:00Z");
    fireEvent.click(screen.getByText("strong matches"));
    expect(props.onShowStrong).toHaveBeenCalled();
    fireEvent.click(screen.getByText("remote"));
    expect(props.onShowRemote).toHaveBeenCalled();
  });

  it("company chips filter the feed", () => {
    const props = setup();
    expect(screen.getByText("Hiring people like you")).toBeTruthy();
    fireEvent.click(screen.getByTitle("Show Shopify jobs"));
    expect(props.onCompany).toHaveBeenCalledWith("Shopify");
  });

  it("extension card: connected shows fields filled", async () => {
    setup();
    expect(await screen.findByText("Autofill is ready")).toBeTruthy();
    expect(screen.getByText(/1,234 application fields/)).toBeTruthy();
  });

  it("extension card: not installed links to the Chrome Web Store", async () => {
    ping.mockResolvedValue("not-installed");
    setup();
    const link = await screen.findByRole("link", { name: "Add to Chrome" });
    expect(link.getAttribute("href")).toContain("chromewebstore.google.com");
  });

  it("extension card: installed but signed out links to connect", async () => {
    ping.mockResolvedValue("installed");
    setup();
    const link = await screen.findByRole("link", { name: "Connect extension" });
    expect(link.getAttribute("href")).toBe("/extension/connect");
  });

  it("alerts switch calls back with the new value and reports failure", async () => {
    const onAlertsChange = vi.fn().mockRejectedValue(new Error("nope"));
    setup(DATA, { onAlertsChange });
    const sw = screen.getByRole("switch");
    expect(sw.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(sw);
    expect(onAlertsChange).toHaveBeenCalledWith(false);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/Couldn't update/));
  });

  it("anonymous data hides the per-user cards", () => {
    setup({ ...DATA, resume: null, progress: null, alerts_enabled: null, feed: { ...DATA.feed, strong_matches: null } });
    expect(screen.queryByText("This week")).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByText("strong matches")).toBeNull();
  });

  it("never uses em dashes in its copy", () => {
    setup();
    expect(document.body.textContent).not.toContain("—");
  });
});
