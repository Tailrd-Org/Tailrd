import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import JobFilterBar, { defaultFilters, type JobFilters } from "../components/JobFilterBar";

vi.mock("../auth/api", () => ({
  default: { get: vi.fn().mockResolvedValue({ data: [] }) },
}));

function renderBar(filters: JobFilters = defaultFilters()) {
  const onChange = vi.fn();
  render(<JobFilterBar filters={filters} onChange={onChange} />);
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
