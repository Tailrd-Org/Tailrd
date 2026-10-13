import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const post = vi.fn();
const get = vi.fn();
vi.mock("../auth/api", () => ({ default: { post: (...a: any) => post(...a), get: (...a: any) => get(...a) } }));
// Stub heavy children so the test stays about wiring.
vi.mock("./ResumeRenderer", () => ({ FittedResume: () => <div /> }));
vi.mock("./ResumeEditor", () => ({ default: () => <div /> }));
vi.mock("./AtsPanel", () => ({ default: () => <div /> }));
vi.mock("./VersionsPanel", () => ({ default: () => <div /> }));

import CustomResumeModal from "./CustomResumeModal";

const analysis = {
  overall_score: 82, ats_score: 82, match_label: "Good match", outcome: "SCORED", keyword_coverage: 50,
  matched_keywords: ["python"], missing_keywords: ["k8s"], strengths: [], weaknesses: [], suggestions: [],
};

describe("CustomResumeModal", () => {
  beforeEach(() => { post.mockReset(); get.mockReset(); });

  it("uses the default /ai/custom-resume-analysis/:id endpoint when no analyze prop", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    post.mockResolvedValue({ data: analysis });
    render(<CustomResumeModal job={{ id: 9, title: "SWE", company: "Acme", url: "u" }} onClose={() => {}} />);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/ai/custom-resume-analysis/9", { resume_id: 3 }));
  });

  it("calls a provided analyze() instead of the default endpoint", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue(analysis);
    render(<CustomResumeModal job={{ title: "SWE", company: "Acme", url: "u" }} onClose={() => {}} analyze={analyze} generate={vi.fn()} jobId={null} />);
    await waitFor(() => expect(analyze).toHaveBeenCalledWith(3));
    expect(post).not.toHaveBeenCalledWith(expect.stringContaining("/ai/custom-resume-analysis"), expect.anything());
  });

  it("shows 'Attach to application' instead of 'Apply Now' at review when onAttach is provided", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue(analysis);
    const rewrite = {
      document: { header: {}, sections: [], theme: {} },
      original_document: { header: {}, sections: [], theme: {} },
      tailored_text: "t", original_text: "o", diff_summary: "",
      original_overall_score: 60, new_overall_score: 75, new_ats_score: 70, new_keyword_coverage: 80,
      version_id: null,
    };
    const generate = vi.fn().mockResolvedValue(rewrite);
    render(<CustomResumeModal job={{ title: "SWE", company: "Acme", url: "u" }} onClose={() => {}} analyze={analyze} generate={generate} jobId={null} onAttach={vi.fn()} />);
    // Step 1 → 2 → 3
    fireEvent.click(await screen.findByRole("button", { name: /Suggest Edits for This Job/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Get Suggestions/i }));
    await waitFor(() => expect(generate).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: /Attach to application/i })).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Apply Now/i })).toBeNull();
  });

  it("never links a closed posting from the review footer", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue(analysis);
    const rewrite = {
      document: { header: {}, sections: [], theme: {} },
      original_document: { header: {}, sections: [], theme: {} },
      tailored_text: "t", original_text: "o", diff_summary: "",
      original_overall_score: 60, new_overall_score: 75, new_ats_score: 70, new_keyword_coverage: 80,
      version_id: null,
    };
    const generate = vi.fn().mockResolvedValue(rewrite);
    render(
      <CustomResumeModal
        job={{ id: 9, title: "SWE", company: "Acme", url: "https://dead.example/9", closed: true }}
        onClose={() => {}}
        analyze={analyze}
        generate={generate}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Suggest Edits for This Job/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Get Suggestions/i }));
    await waitFor(() => expect(generate).toHaveBeenCalled());
    const closed = await screen.findByRole("button", { name: /Posting closed/i });
    expect((closed as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("link", { name: /Apply Now/i })).toBeNull();
    expect(document.querySelector("a[href='https://dead.example/9']")).toBeNull();
  });

  it("keeps Apply Now for an open posting", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue(analysis);
    const rewrite = {
      document: { header: {}, sections: [], theme: {} },
      original_document: { header: {}, sections: [], theme: {} },
      tailored_text: "t", original_text: "o", diff_summary: "",
      original_overall_score: 60, new_overall_score: 75, new_ats_score: 70, new_keyword_coverage: 80,
      version_id: null,
    };
    const generate = vi.fn().mockResolvedValue(rewrite);
    render(
      <CustomResumeModal
        job={{ id: 9, title: "SWE", company: "Acme", url: "https://live.example/9" }}
        onClose={() => {}}
        analyze={analyze}
        generate={generate}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Suggest Edits for This Job/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Get Suggestions/i }));
    const link = await screen.findByRole("link", { name: /Apply Now/i });
    expect(link.getAttribute("href")).toBe("https://live.example/9");
    expect(screen.queryByRole("button", { name: /Posting closed/i })).toBeNull();
  });

  it("renders honest gaps and figures-to-verify at review", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue(analysis);
    const rewrite = {
      document: { header: {}, sections: [], theme: {} },
      original_document: { header: {}, sections: [], theme: {} },
      tailored_text: "t", original_text: "o", diff_summary: "",
      original_overall_score: 60, new_overall_score: 75, new_ats_score: 70, new_keyword_coverage: 80,
      version_id: null,
      changes: ["Reordered sections to lead with the most relevant experience"],
      gaps: ["Role requires Kubernetes; not shown"],
      figures_to_verify: ["50%"],
    };
    const generate = vi.fn().mockResolvedValue(rewrite);
    render(<CustomResumeModal job={{ title: "SWE", company: "Acme", url: "u" }} onClose={() => {}} analyze={analyze} generate={generate} jobId={null} />);
    fireEvent.click(await screen.findByRole("button", { name: /Suggest Edits for This Job/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Get Suggestions/i }));
    await waitFor(() => expect(generate).toHaveBeenCalled());
    expect(await screen.findByText(/Gaps to consider/i)).toBeTruthy();
    expect(screen.getByText(/Role requires Kubernetes/i)).toBeTruthy();
    expect(screen.getByText(/Verify these figures/i)).toBeTruthy();
    expect(screen.getByText(/Reordered sections/i)).toBeTruthy();
  });

  it("shows the fit score as an integer with the API's band, never X.X or an 80 / 60 label", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue(analysis);
    const { container } = render(<CustomResumeModal job={{ title: "SWE", company: "Acme", url: "u" }} onClose={() => {}} analyze={analyze} generate={vi.fn()} jobId={null} />);
    await screen.findByRole("button", { name: /Suggest Edits for This Job/i });
    expect(await screen.findByText(/Your Resume is a Good Match/)).toBeTruthy();
    const gauge = container.querySelector(".ai-gauge2");
    expect(gauge?.textContent).toContain("82");
    expect(gauge?.textContent).toContain("Good match");
    expect(gauge?.textContent).not.toContain("8.2");
    expect(gauge?.textContent).not.toMatch(/STRONG|GOOD|FAIR/);
  });

  it("renders a result with no score without a number (We couldn't score this)", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue({
      ...analysis, overall_score: null, ats_score: null, outcome: "NEEDS_REVIEW", match_label: "We couldn't score this",
    });
    const { container } = render(<CustomResumeModal job={{ title: "SWE", company: "Acme", url: "u" }} onClose={() => {}} analyze={analyze} generate={vi.fn()} jobId={null} />);
    await screen.findByRole("button", { name: /Suggest Edits for This Job/i });
    expect((await screen.findAllByText(/We couldn't score this/)).length).toBeGreaterThan(0);
    const gauge = container.querySelector(".ai-gauge2");
    expect(gauge?.textContent).not.toMatch(/\d/);
    expect(container.textContent).not.toMatch(/null|NaN|ATS null/);
  });

  it("states the before and after fit scores as integers at review", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue(analysis);
    const rewrite = {
      document: { header: {}, sections: [], theme: {} },
      original_document: { header: {}, sections: [], theme: {} },
      tailored_text: "t", original_text: "o", diff_summary: "",
      original_overall_score: 60, new_overall_score: 86, new_ats_score: 86, new_keyword_coverage: 80,
      version_id: null,
    };
    const generate = vi.fn().mockResolvedValue(rewrite);
    const { container } = render(<CustomResumeModal job={{ title: "SWE", company: "Acme", url: "u" }} onClose={() => {}} analyze={analyze} generate={generate} jobId={null} />);
    fireEvent.click(await screen.findByRole("button", { name: /Suggest Edits for This Job/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Get Suggestions/i }));
    expect(await screen.findByText("With these suggestions your fit score goes from 60 to 86")).toBeTruthy();
    // 86 is Strong under 85 / 70 / 55.
    expect(container.querySelector(".ai-side-gauge")?.textContent).toContain("Strong match");
  });

  it("does not crash or print null when the review scores are missing", async () => {
    get.mockResolvedValue({ data: [{ id: 3, name: "CV", is_primary: true }] });
    const analyze = vi.fn().mockResolvedValue(analysis);
    const rewrite = {
      document: { header: {}, sections: [], theme: {} },
      original_document: { header: {}, sections: [], theme: {} },
      tailored_text: "t", original_text: "o", diff_summary: "",
      original_overall_score: null, new_overall_score: null, new_ats_score: null, new_keyword_coverage: 80,
      version_id: null,
    };
    const generate = vi.fn().mockResolvedValue(rewrite);
    const { container } = render(<CustomResumeModal job={{ title: "SWE", company: "Acme", url: "u" }} onClose={() => {}} analyze={analyze} generate={generate} jobId={null} />);
    fireEvent.click(await screen.findByRole("button", { name: /Suggest Edits for This Job/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Get Suggestions/i }));
    expect(await screen.findByText(/Suggestions ready/)).toBeTruthy();
    expect(container.textContent).not.toMatch(/null|NaN/);
  });
});
