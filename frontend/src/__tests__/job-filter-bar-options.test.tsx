import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import JobFilterBar, { defaultFilters, type JobFilters } from "../components/JobFilterBar";

vi.mock("../auth/api", () => ({
  default: { get: vi.fn().mockResolvedValue({ data: [] }) },
}));

function renderBar(filters: JobFilters = defaultFilters(), available?: readonly string[] | null) {
  const onChange = vi.fn();
  render(<JobFilterBar filters={filters} onChange={onChange} availableExperienceLevels={available} />);
  return onChange;
}

function openPanel(name: RegExp) {
  fireEvent.click(screen.getByRole("button", { name }));
  return screen.getByRole("dialog");
}

/** Each checkbox's label text and state, in render order. */
function boxes(panel: HTMLElement) {
  return within(panel).getAllByRole("checkbox").map((box) => ({
    label: box.closest("label")?.textContent?.trim(),
    checked: (box as HTMLInputElement).checked,
  }));
}

describe("JobFilterBar experience levels", () => {
  it("offers Jobright's six levels, the default two ticked", () => {
    renderBar();
    const panel = openPanel(/experience level/i);
    expect(boxes(panel)).toEqual([
      { label: "Intern/New Grad", checked: true },
      { label: "Entry Level", checked: true },
      { label: "Mid Level", checked: false },
      { label: "Senior Level", checked: false },
      { label: "Lead/Staff", checked: false },
      { label: "Director/Executive", checked: false },
    ]);
  });

  it("widens the filter to a level that is ticked", () => {
    const onChange = renderBar();
    const panel = openPanel(/experience level/i);
    fireEvent.click(within(panel).getByLabelText("Senior Level"));
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ experience_level: ["intern_new_grad", "entry", "senior"] }),
    );
  });

  it("confirming with nothing ticked applies the default levels, never every level", () => {
    const onChange = renderBar({ ...defaultFilters(), experience_level: ["director"] });
    const panel = openPanel(/experience level/i);
    fireEvent.click(within(panel).getByLabelText("Director/Executive"));
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ experience_level: ["intern_new_grad", "entry"] }),
    );
  });

  it("Reset goes back to the default levels", () => {
    const onChange = renderBar({ ...defaultFilters(), experience_level: ["mid", "lead"] });
    const panel = openPanel(/experience level/i);
    fireEvent.click(within(panel).getByRole("button", { name: /reset/i }));
    expect(boxes(panel).filter((b) => b.checked).map((b) => b.label)).toEqual([
      "Intern/New Grad",
      "Entry Level",
    ]);
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ experience_level: ["intern_new_grad", "entry"] }),
    );
  });
});

/**
 * Until every level is ingested, production stores only internships and new grads. The
 * page hands the bar the options that have jobs (GET /jobs/levels), and the bar offers
 * those and nothing else: an empty option is a silent dead end.
 */
describe("JobFilterBar experience levels the catalogue holds", () => {
  const STUDENT_ONLY = ["intern_new_grad"];

  it("offers only the options that have jobs, ticked as filtered", () => {
    renderBar({ ...defaultFilters(), experience_level: ["intern_new_grad"] }, STUDENT_ONLY);
    const panel = openPanel(/experience level/i);
    expect(boxes(panel)).toEqual([{ label: "Intern/New Grad", checked: true }]);
  });

  it("keeps the options' order and labels when several have jobs", () => {
    renderBar(
      { ...defaultFilters(), experience_level: ["intern_new_grad"] },
      ["director", "entry", "intern_new_grad"],
    );
    const panel = openPanel(/experience level/i);
    expect(boxes(panel).map((b) => b.label)).toEqual(["Intern/New Grad", "Entry Level", "Director/Executive"]);
  });

  it("offers all six while the options are not known", () => {
    renderBar(defaultFilters(), null);
    const panel = openPanel(/experience level/i);
    expect(boxes(panel)).toHaveLength(6);
  });

  it("drops a ticked level whose option is hidden when confirmed", () => {
    // The saved default includes Entry Level, which has no jobs and so no checkbox.
    const onChange = renderBar(defaultFilters(), STUDENT_ONLY);
    const panel = openPanel(/experience level/i);
    expect(within(panel).queryByLabelText("Entry Level")).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ experience_level: ["intern_new_grad"] }));
  });

  it("confirming with nothing ticked applies the default levels that exist", () => {
    const onChange = renderBar({ ...defaultFilters(), experience_level: ["intern_new_grad"] }, STUDENT_ONLY);
    const panel = openPanel(/experience level/i);
    fireEvent.click(within(panel).getByLabelText("Intern/New Grad"));
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ experience_level: ["intern_new_grad"] }));
  });

  it("Reset goes back to the default levels that exist", () => {
    const onChange = renderBar(
      { ...defaultFilters(), experience_level: ["intern_new_grad", "mid"] },
      ["intern_new_grad", "mid"],
    );
    const panel = openPanel(/experience level/i);
    fireEvent.click(within(panel).getByRole("button", { name: /reset/i }));
    expect(boxes(panel).filter((b) => b.checked).map((b) => b.label)).toEqual(["Intern/New Grad"]);
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ experience_level: ["intern_new_grad"] }));
  });

  it("the All Filters panel offers the same options and confirms the same levels", () => {
    const onChange = renderBar(defaultFilters(), STUDENT_ONLY);
    const panel = openPanel(/all filters/i);
    expect(within(panel).getByLabelText("Intern/New Grad")).toBeInTheDocument();
    for (const hidden of ["Entry Level", "Mid Level", "Senior Level", "Lead/Staff", "Director/Executive"]) {
      expect(within(panel).queryByLabelText(hidden)).toBeNull();
    }
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ experience_level: ["intern_new_grad"] }));
  });
});

describe("JobFilterBar job functions", () => {
  it("lists the 20 career families by label", () => {
    renderBar();
    const labels = boxes(openPanel(/job function/i)).map((b) => b.label);
    expect(labels).toHaveLength(20);
    expect(labels[0]).toBe("Software Engineering");
    expect(labels).toContain("Healthcare, Clinical");
    expect(labels).toContain("Skilled Trades, Construction & Manufacturing");
    expect(labels[19]).toBe("Other");
  });

  it("sends family slugs, never labels", () => {
    const onChange = renderBar();
    const panel = openPanel(/job function/i);
    fireEvent.click(within(panel).getByLabelText("Data, AI & Analytics"));
    fireEvent.click(within(panel).getByLabelText("Healthcare, Clinical"));
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ career_family: ["data_ai_analytics", "healthcare_clinical"] }),
    );
  });

  it("shows a saved family ticked, and Reset clears the families", () => {
    const onChange = renderBar({ ...defaultFilters(), career_family: ["legal"] });
    const panel = openPanel(/job function/i);
    expect(boxes(panel).filter((b) => b.checked).map((b) => b.label)).toEqual(["Legal"]);
    fireEvent.click(within(panel).getByRole("button", { name: /reset/i }));
    fireEvent.click(within(panel).getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ career_family: [] }));
  });
});
