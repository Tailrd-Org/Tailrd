/**
 * Calls the backend résumé-tailoring endpoints (POST /api/tailor-resume,
 * POST /api/render-resume) from the service worker. Mirrors api/aiFill.ts:
 * authedRequest handles auth + silent token refresh. The server returns
 * snake_case; mapTailorResponse normalizes to the camelCase TailorResult the
 * UI consumes (a pure function, unit-tested).
 */
import type { JobContext, ResumeDoc, TailorResult } from "../shared/types";
import { authedRequest } from "./client";

interface TailorApiResponse {
  document: ResumeDoc;
  original_overall_score: number | null;
  new_overall_score: number | null;
  new_ats_score: number | null;
  new_keyword_coverage: number | null;
  match_label?: string;
  reasons?: string[];
  matched_keywords: string[];
  missing_keywords: string[];
  diff_summary: string;
}

export function buildTailorRequestBody(
  resumeId: number | null,
  jobContext: JobContext,
  sections?: string[],
  addKeywords?: string[] | null
): {
  resume_id: number | null;
  job_description: string;
  job_title: string;
  company: string;
  sections: string[] | null;
  add_keywords: string[] | null;
} {
  return {
    resume_id: resumeId,
    job_description: jobContext.jobDescription,
    job_title: jobContext.jobTitle,
    company: jobContext.company,
    sections: sections ?? null,
    add_keywords: addKeywords ?? null,
  };
}

export function mapTailorResponse(r: TailorApiResponse): TailorResult {
  return {
    document: r.document,
    originalScore: r.original_overall_score ?? null,
    newScore: r.new_overall_score ?? null,
    atsScore: r.new_ats_score ?? null,
    keywordCoverage: r.new_keyword_coverage ?? null,
    matchedKeywords: r.matched_keywords ?? [],
    missingKeywords: r.missing_keywords ?? [],
    diffSummary: r.diff_summary ?? "",
    matchLabel: r.match_label ?? "",
    reasons: r.reasons ?? [],
  };
}

export async function tailorResume(
  resumeId: number | null,
  jobContext: JobContext,
  sections?: string[],
  addKeywords?: string[] | null
): Promise<TailorResult> {
  const raw = await authedRequest<TailorApiResponse>("/api/tailor-resume", {
    method: "POST",
    body: JSON.stringify(buildTailorRequestBody(resumeId, jobContext, sections, addKeywords)),
  });
  return mapTailorResponse(raw);
}

export async function renderResume(
  document: ResumeDoc,
  filename?: string
): Promise<{ dataBase64: string; name: string; contentType: string }> {
  const res = await authedRequest<{ data_base64: string; name: string; content_type: string }>(
    "/api/render-resume",
    { method: "POST", body: JSON.stringify({ document, filename: filename ?? null }) }
  );
  return { dataBase64: res.data_base64, name: res.name, contentType: res.content_type };
}
