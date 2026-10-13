import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import FitBadge from "./FitBadge";

describe("FitBadge (job card)", () => {
  it("SCORED: 'Fit 76' with the band from the API, never a percent sign", () => {
    const { container } = render(
      <FitBadge job={{ match_score: 76, match_band: "Good match", match_outcome: "SCORED" }} />,
    );
    expect(screen.getByText("Fit 76")).toBeTruthy();
    expect(screen.getByText("Good match")).toBeTruthy();
    expect(container.textContent).not.toContain("%");
    expect(container.textContent).not.toMatch(/STRONG|GOOD|FAIR/);
  });

  it("uses the API band, not an 80 / 60 cut", () => {
    render(<FitBadge job={{ match_score: 82, match_band: "Good match", match_outcome: "SCORED" }} />);
    expect(screen.getByText("Good match")).toBeTruthy();
    expect(screen.queryByText(/strong/i)).toBeNull();
  });

  it("SCORED_LOW_CONFIDENCE: the band only, no number", () => {
    const { container } = render(
      <FitBadge job={{ match_score: 0, match_band: "Partial match", match_outcome: "SCORED_LOW_CONFIDENCE" }} />,
    );
    expect(screen.getByText("Partial match")).toBeTruthy();
    expect(container.textContent).not.toMatch(/\d/);
    expect(container.textContent).not.toContain("Fit");
  });

  it("GATE_FAIL: Not eligible, pointing at the reason in the job", () => {
    const { container } = render(
      <FitBadge job={{ match_score: 0, match_band: null, match_outcome: "GATE_FAIL" }} />,
    );
    expect(screen.getByText("Not eligible")).toBeTruthy();
    expect(container.querySelector("[title]")?.getAttribute("title")).toMatch(/reason/i);
    expect(container.textContent).not.toMatch(/\d/);
  });

  it("NEEDS_REVIEW: We couldn't score this, no number", () => {
    const { container } = render(
      <FitBadge job={{ match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" }} />,
    );
    expect(screen.getByText("We couldn't score this")).toBeTruthy();
    expect(container.textContent).not.toMatch(/\d/);
  });

  it("renders nothing for a job not scored yet", () => {
    const { container } = render(<FitBadge job={{ match_score: 0, match_band: null, match_outcome: null }} />);
    expect(container.innerHTML).toBe("");
  });
});
