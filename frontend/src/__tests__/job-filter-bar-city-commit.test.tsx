import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import JobFilterBar, { type JobFilters } from "../components/JobFilterBar";

vi.mock("../auth/api", () => ({
  default: { get: vi.fn().mockResolvedValue({ data: [] }) },
}));

const EMPTY: JobFilters = {
  country: "CA",
  location: [],
  work_type: [],
  career_family: [],
  experience_level: ["intern_new_grad", "entry"],
  date_posted: "",
};

function openCountryPanel() {
  fireEvent.click(screen.getByRole("button", { name: /country/i }));
}

describe("JobFilterBar city input commit", () => {
  let onChange: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    onChange = vi.fn();
    render(<JobFilterBar filters={EMPTY} onChange={onChange} />);
    openCountryPanel();
  });

  it("Confirm commits typed-but-unentered city text as a tag", () => {
    // The reported bug: type a city, never press Enter, hit Confirm →
    // the filter silently applied NO location at all.
    fireEvent.change(
      screen.getByLabelText(/type a city name/i),
      { target: { value: "Ottawa" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ location: ["Ottawa"] }),
    );
  });

  it("the All-locations checkbox unchecks as soon as text is typed", () => {
    const checkbox = screen.getByRole("checkbox", { name: /all locations within/i });
    expect((checkbox as HTMLInputElement).checked).toBe(true);
    fireEvent.change(
      screen.getByLabelText(/type a city name/i),
      { target: { value: "Ott" } },
    );
    expect((checkbox as HTMLInputElement).checked).toBe(false);
  });

  it("checking All-locations clears both tags and pending text", () => {
    const input = screen.getByLabelText(/type a city name/i);
    fireEvent.change(input, { target: { value: "Ottawa" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.change(input, { target: { value: "Cal" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /all locations within/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ location: [] }),
    );
  });

  it("Enter still creates a tag and Confirm sends it once", () => {
    const input = screen.getByLabelText(/type a city name/i);
    fireEvent.change(input, { target: { value: "Ottawa" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ location: ["Ottawa"] }),
    );
  });
});
