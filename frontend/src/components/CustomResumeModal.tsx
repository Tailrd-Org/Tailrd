import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AxiosInstance } from "axios";
import api from "../auth/api";
import { FittedResume } from "./ResumeRenderer";
import ResumeEditor from "./ResumeEditor";
import AtsPanel from "./AtsPanel";
import VersionsPanel from "./VersionsPanel";
import { DEFAULT_THEME, type ResumeDocument } from "../lib/resumeDocument";
import { addSkills, useDocumentHistory } from "../lib/resumeEdit";
import { analyzeKeywords, heatmapTerms } from "../lib/keywordMatch";
import { bandFor, bandTone } from "../lib/fit";
import { changedStrings } from "../lib/resumeDiff";
import type { HighlightMode, HighlightState } from "./ResumeRenderer";
import { downloadResumeDocx, printResume } from "../lib/resumeExport";
import "./ai-flow.css";

const EMPTY_DOCUMENT: ResumeDocument = {
  header: { name: "", email: "", phone: "", location: "", linkedin_url: "", github_url: "", other_link: "" },
  sections: [],
  theme: DEFAULT_THEME,
};

export interface AIJob {
  id?: number;
  title: string;
  company: string;
  url: string;
  /** The posting no longer accepts applications: never offer its dead link. */
  closed?: boolean;
}

export interface AttachFile {
  dataBase64: string;
  filename: string;
  contentType: string;
}

interface CustomResumeModalProps {
  job: AIJob;
  onClose: () => void;
  /** Override the analysis fetch (default: POST /ai/custom-resume-analysis/:jobId). */
  analyze?: (resumeId: number | null) => Promise<Analysis>;
  /** Override the rewrite fetch (default: POST /ai/custom-resume/:jobId). */
  generate?: (resumeId: number | null, sections: string[], keywords: string[]) => Promise<RewriteResult>;
  /** Job id threaded to VersionsPanel/ResumeEditor (default: job.id ?? null). */
  jobId?: number | null;
  /** When set, the footer shows "Attach to application" instead of "Apply Now". */
  onAttach?: () => Promise<void>;
  /**
   * Axios instance for every request (résumé list/detail + default AI calls).
   * Defaults to the web app's auth client. The extension embed passes a
   * token-bridged client so calls work inside a third-party iframe (and a 401
   * never redirects the iframe to /sign-in).
   */
  apiClient?: AxiosInstance;
}

interface ResumeOption {
  id: number;
  name: string;
  is_primary: boolean;
}

// Scores come from the fit engine (Task 9.3): null unless the result is SCORED, and
// match_label is the band, or "We couldn't score this" / "Not eligible" when there is none.
interface Analysis {
  overall_score: number | null;
  ats_score: number | null;
  match_label: string;
  outcome?: string;
  keyword_coverage: number | null;
  matched_keywords: string[];
  missing_keywords: string[];
  strengths: string[];
  weaknesses: string[];
  suggestions: string[];
}

interface RewriteResult {
  document: ResumeDocument;
  original_document: ResumeDocument;
  tailored_text: string;
  original_text: string;
  diff_summary: string;
  original_overall_score: number | null;
  new_overall_score: number | null;
  new_ats_score: number | null;
  new_keyword_coverage: number | null;
  // Task 9.3: the tailored document's own outcome and label (band or outcome words).
  original_outcome?: string | null;
  outcome?: string | null;
  original_match_label?: string;
  match_label?: string;
  version_id?: number | null;
  changes?: string[];
  gaps?: string[];
  figures_to_verify?: string[];
}

const ALL_SECTIONS = ["Skills", "Work Experience", "Projects", "Education"] as const;

// Inline icons
const Check = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
);
const Spark = () => (
  <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8z" /></svg>
);
const FileIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 3v4a1 1 0 0 0 1 1h4" /><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z" /></svg>
);
const ThumbUp = () => (
  <svg viewBox="0 0 24 24" fill="currentColor"><path d="M2 21h4V9H2v12zM23 10c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-1z" /></svg>
);

const LOGO_PALETTE = ["#533afd", "#F97316", "#0EA5E9", "#22C55E", "#E11D48", "#A855F7", "#0891B2"];
function logoColor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return LOGO_PALETTE[h % LOGO_PALETTE.length];
}

function errorMessage(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: string }; status?: number } })?.response;
  if (detail?.status === 400) return detail.data?.detail || "Upload a resume first to get suggestions.";
  if (detail?.status === 503) return "AI is temporarily unavailable. Please try again in a moment.";
  return detail?.data?.detail || fallback;
}

// Semicircular rainbow fit-score gauge: the engine's integer (0-100) and its band.
// With no score (low confidence, not eligible, not scored) it shows the words only.
function Gauge({ score, label, size = "lg" }: { score: number | null; label: string; size?: "lg" | "sm" }) {
  const r = 52;
  const cx = 60;
  const cy = 62;
  const hasScore = typeof score === "number";
  const frac = hasScore ? Math.max(0, Math.min(100, score)) / 100 : 0;
  const len = Math.PI * r; // semicircle arc length
  const angle = Math.PI * (1 - frac); // 180° (empty) → 0° (full)
  const knobX = cx + r * Math.cos(angle);
  const knobY = cy - r * Math.sin(angle);
  const arc = `M${cx - r},${cy} A${r},${r} 0 0 1 ${cx + r},${cy}`;
  return (
    <div className={`ai-gauge2 ${size}`}>
      <svg viewBox="0 0 120 78" className="ai-gauge2-svg">
        <defs>
          <linearGradient id="ai-gauge-grad" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#f97316" />
            <stop offset="45%" stopColor="#facc15" />
            <stop offset="100%" stopColor="#22c55e" />
          </linearGradient>
        </defs>
        <path d={arc} fill="none" stroke="#ece9f7" strokeWidth="9" strokeLinecap="round" />
        <path
          d={arc}
          fill="none"
          stroke="url(#ai-gauge-grad)"
          strokeWidth="9"
          strokeLinecap="round"
          strokeDasharray={`${frac * len} ${len}`}
        />
        {hasScore && <circle cx={knobX} cy={knobY} r="6" fill="#fff" stroke="#1f2937" strokeWidth="2.5" />}
        {hasScore && <text x="60" y="56" textAnchor="middle" className="ai-gauge2-num">{score}</text>}
      </svg>
      <div className="ai-gauge2-lbl">{label}</div>
    </div>
  );
}

export default function CustomResumeModal({
  job, onClose, analyze, generate: generateProp, jobId, onAttach, apiClient,
}: CustomResumeModalProps) {
  const client = apiClient ?? api;
  const effJobId = jobId !== undefined ? jobId : (job.id ?? null);
  const doAnalyze = analyze ?? ((rid: number | null) =>
    client.post<Analysis>(`/ai/custom-resume-analysis/${job.id}`, { resume_id: rid }).then((r) => r.data));
  const doGenerate = generateProp ?? ((rid: number | null, secs: string[], kws: string[]) =>
    client.post<RewriteResult>(`/ai/custom-resume/${job.id}`, { resume_id: rid, sections: secs, add_keywords: kws }).then((r) => r.data));

  const [step, setStep] = useState<1 | 2 | 3>(1);

  const [resumes, setResumes] = useState<ResumeOption[]>([]);
  const [resumeId, setResumeId] = useState<number | null>(null);
  const [availableSections, setAvailableSections] = useState<string[]>([]);

  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [loadingAnalysis, setLoadingAnalysis] = useState(true);
  const [analysisError, setAnalysisError] = useState("");

  const [sections, setSections] = useState<Set<string>>(new Set());
  const [keywords, setKeywords] = useState<Set<string>>(new Set());

  const [rewrite, setRewrite] = useState<RewriteResult | null>(null);
  const [loadingRewrite, setLoadingRewrite] = useState(false);
  const [rewriteError, setRewriteError] = useState("");
  const [copied, setCopied] = useState(false);
  const previewRef = useRef<HTMLDivElement>(null);

  // Visual editor: edited document + undo/redo history, seeded on generate.
  const [editing, setEditing] = useState(false);
  // Default to the "what changed" layer so the review opens showing what the
  // rewrite did (the printed PDF/DOCX stay clean, see printResume + schema DOCX).
  const [highlightMode, setHighlightMode] = useState<HighlightMode>("changed");
  const {
    doc: editedDoc,
    set: setEditedDoc,
    reset: resetEditedDoc,
    undo,
    redo,
    canUndo,
    canRedo,
  } = useDocumentHistory(EMPTY_DOCUMENT);

  // Keyboard undo/redo while the editor is open.
  useEffect(() => {
    if (!editing) return;
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (!mod) return;
      const k = e.key.toLowerCase();
      if (k === "z") {
        e.preventDefault();
        e.shiftKey ? redo() : undo();
      } else if (k === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing, undo, redo]);

  // Load the analysis + resume sections for a given resume.
  const loadForResume = useCallback(
    async (rid: number | null) => {
      setLoadingAnalysis(true);
      setAnalysisError("");
      setAnalysis(null);
      setRewrite(null);
      setStep(1);
      try {
        const [analysisData, detailRes] = await Promise.all([
          doAnalyze(rid),
          rid ? client.get(`/resumes/${rid}`) : Promise.resolve(null),
        ]);
        setAnalysis(analysisData);
        setKeywords(new Set());

        const profile = detailRes?.data?.profile;
        const avail = profile
          ? ALL_SECTIONS.filter((s) => {
            if (s === "Skills") return (profile.skills ?? []).length > 0;
            if (s === "Work Experience") return (profile.experience ?? []).length > 0;
            if (s === "Projects") return (profile.projects ?? []).length > 0;
            if (s === "Education") return (profile.education ?? []).length > 0;
            return false;
          })
          : ["Skills", "Work Experience", "Projects"];
        const finalAvail = avail.length ? avail : ["Skills", "Work Experience", "Projects"];
        setAvailableSections(finalAvail);
        setSections(new Set(finalAvail));
      } catch (err) {
        setAnalysisError(errorMessage(err, "Couldn't analyze this job. Please try again."));
      } finally {
        setLoadingAnalysis(false);
      }
    },
    [job.id]
  );

  // On mount: load resumes, then analyze with the default (primary) resume.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await client.get<ResumeOption[]>("/resumes");
        if (cancelled) return;
        setResumes(res.data);
        const def = res.data.find((r) => r.is_primary) ?? res.data[0];
        const rid = def?.id ?? null;
        setResumeId(rid);
        await loadForResume(rid);
      } catch {
        if (!cancelled) {
          setResumes([]);
          await loadForResume(null);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadForResume]);

  function onPickResume(id: number) {
    setResumeId(id);
    void loadForResume(id);
  }

  async function generate() {
    setLoadingRewrite(true);
    setRewriteError("");
    setStep(3);
    try {
      const data = await doGenerate(resumeId, [...sections], [...keywords]);
      setRewrite(data);
      resetEditedDoc(data.document);
      setEditing(false);
    } catch (err) {
      setRewriteError(errorMessage(err, "Couldn't prepare suggestions. Please try again."));
    } finally {
      setLoadingRewrite(false);
    }
  }

  const slug = job.company.toLowerCase().replace(/\s+/g, "-") || "company";

  function downloadPdf() {
    if (!rewrite || !previewRef.current) return;
    printResume(previewRef.current, editedDoc.theme.page_size);
  }

  function downloadDocxFile() {
    if (!rewrite) return;
    void downloadResumeDocx(editedDoc, `resume-${slug}.docx`);
  }

  function applySkills(skills: string[]) {
    setEditedDoc(addSkills(editedDoc, skills));
  }

  function copy() {
    if (!rewrite) return;
    navigator.clipboard.writeText(rewrite.tailored_text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const matchHeadline = useMemo(() => {
    if (!analysis) return "";
    // The band comes from the API (85 / 70 / 55); with no band, its own words.
    const tone = bandTone(analysis.match_label);
    if (tone === "strong") return "Your Resume is a Strong Match. Here Are a Few Suggestions";
    if (tone === "good") return "Your Resume is a Good Match. Here's What You Could Strengthen";
    if (tone === "partial") return "Your Resume is a Partial Match. Here's Where to Start";
    if (tone === "limited") return "Your Resume is a Limited Match. Here's Where to Start";
    return analysis.match_label || "Here's Where to Start";
  }, [analysis]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="ai-modal" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="ai-modal-head">
          <div className="ai-head-top">
            <button className="ai-modal-close" onClick={onClose} aria-label="Close">✕</button>
            <span className="ai-modal-title">Suggest Edits for This Job</span>
            <span className="ai-modal-sub">{job.title} · {job.company}</span>
          </div>
          <div className="ai-steps">
            <div className={`ai-step ${step === 1 ? "active" : step > 1 ? "done" : ""}`}>
              <span className="ai-step-num">{step > 1 ? "✓" : "1"}</span> See Your Difference
            </div>
            <div className={`ai-step-line ${step > 1 ? "filled" : ""}`} />
            <div className={`ai-step ${step === 2 ? "active" : step > 2 ? "done" : ""}`}>
              <span className="ai-step-num">{step > 2 ? "✓" : "2"}</span> Pick What to Improve
            </div>
            <div className={`ai-step-line ${step > 2 ? "filled" : ""}`} />
            <div className={`ai-step ${step === 3 ? "active" : ""}`}>
              <span className="ai-step-num">3</span> Review the Suggestions
            </div>
          </div>
        </div>

        {/* Body */}
        <div className="ai-modal-body">
          {step === 1 && renderStep1()}
          {step === 2 && renderStep2()}
          {step === 3 && renderStep3()}
        </div>

        {/* Footer */}
        {renderFooter()}
      </div>
    </div>
  );

  function renderStep1() {
    if (loadingAnalysis) {
      return (
        <div className="ai-loading">
          <div className="ai-spinner" />
          <div className="ai-loading-title">Analyzing your match…</div>
        </div>
      );
    }
    if (analysisError) {
      return (
        <div className="ai-error-box">
          <p>{analysisError}</p>
          <button className="ai-btn ai-btn-soft" onClick={() => loadForResume(resumeId)}>Try again</button>
        </div>
      );
    }
    if (!analysis) return null;
    const total = analysis.matched_keywords.length + analysis.missing_keywords.length;
    const fullCoverage = analysis.missing_keywords.length === 0;
    return (
      <>
        <div className="ai-s1-head">
          <div>
            <h2 className="ai-s1-title">{matchHeadline}</h2>
            <span className="ai-info">
              <i className="info-dot">i</i>
              {fullCoverage
                ? "Strong keyword coverage. A few tweaks will sharpen it further."
                : "You're on the right track, but some keywords are still missing."}
            </span>
          </div>
          <Gauge score={analysis.overall_score} label={analysis.match_label} />
        </div>

        <div className="ai-cmp">
          {/* Overview row: the job vs your resume */}
          <div className="ai-cmp-label">Overview</div>
          <div className="ai-cmp-cell ai-cmp-pair">
            <div className="ai-cmp-logo" style={{ background: logoColor(job.company) }}>
              {job.company.charAt(0).toUpperCase()}
            </div>
            <div className="ai-cmp-pair-text">
              <span className="ai-cmp-eyebrow">The job</span>
              <span className="ai-cmp-name" title={job.title}>{job.title}</span>
              <span className="ai-cmp-meta">{job.company}</span>
            </div>
          </div>
          <div className="ai-cmp-cell ai-cmp-pair">
            <div className="ai-cmp-logo doc"><FileIcon /></div>
            <div className="ai-cmp-pair-text">
              <span className="ai-cmp-eyebrow">Your resume</span>
              <select
                className="ai-resume-pick"
                value={resumeId ?? ""}
                onChange={(e) => onPickResume(Number(e.target.value))}
                disabled={resumes.length === 0}
              >
                {resumes.length === 0 && <option>No resume uploaded</option>}
                {resumes.map((r) => (
                  <option key={r.id} value={r.id}>{r.name}{r.is_primary ? " (default)" : ""}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Keywords row */}
          <div className={`ai-cmp-label${fullCoverage ? "" : " warn"}`}>
            <span>Job Keywords <strong>({analysis.matched_keywords.length}/{total})</strong></span>
            <span className="ai-cmp-sub">
              {/* No "ATS" figure: it equals the fit score (D-123), the same number twice. */}
              {analysis.keyword_coverage != null && `${analysis.keyword_coverage}% coverage`}
            </span>
            {!fullCoverage && <span className="ai-warn-badge" aria-hidden>!</span>}
          </div>
          <div className="ai-cmp-cell ai-cmp-wide warm">
            <div className="ai-chips">
              {analysis.matched_keywords.map((k) => (
                <span key={k} className="ai-chip on"><ThumbUp />{k}</span>
              ))}
              {analysis.missing_keywords.map((k) => (
                <span key={k} className="ai-chip miss">{k}</span>
              ))}
              {total === 0 && <span className="ai-cmp-meta">No keywords detected for this role.</span>}
            </div>
          </div>

          {/* AI insights row */}
          {analysis.suggestions.length > 0 && (
            <>
              <div className="ai-cmp-label">AI suggestions</div>
              <div className="ai-cmp-cell ai-cmp-wide">
                <ul className="ai-insights">
                  {analysis.suggestions.slice(0, 4).map((s, i) => <li key={i}>{s}</li>)}
                </ul>
              </div>
            </>
          )}
        </div>
      </>
    );
  }

  function renderStep2() {
    const missing = analysis?.missing_keywords ?? [];
    return (
      <div className="ai-align">
        <div>
          <h3 className="ai-col-title">1. Choose sections for suggestions</h3>
          <div className="ai-check-list">
            {availableSections.map((s) => {
              const on = sections.has(s);
              return (
                <label key={s} className={`ai-check ${on ? "checked" : ""}`}>
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => {
                      const next = new Set(sections);
                      on ? next.delete(s) : next.add(s);
                      setSections(next);
                    }}
                  />
                  <span className="ai-check-box"><Check /></span>
                  {s}
                </label>
              );
            })}
          </div>
        </div>
        <div>
          <div className="ai-kw-head">
            <h3 className="ai-col-title" style={{ margin: 0 }}>
              2. Add missing skill keywords ({keywords.size}/{missing.length})
            </h3>
            {missing.length > 0 && (
              <button
                className="ai-link-btn"
                onClick={() => setKeywords(keywords.size === missing.length ? new Set() : new Set(missing))}
              >
                {keywords.size === missing.length ? "Clear" : "Select all"}
              </button>
            )}
          </div>
          <div className="ai-kw-grid">
            {missing.length === 0 && <span className="ai-ov-meta">No missing keywords. Nice!</span>}
            {missing.map((k) => {
              const on = keywords.has(k);
              return (
                <button
                  key={k}
                  className={`ai-kw-pick ${on ? "checked" : ""}`}
                  onClick={() => {
                    const next = new Set(keywords);
                    on ? next.delete(k) : next.add(k);
                    setKeywords(next);
                  }}
                >
                  <span className="ai-check-box" style={{ width: 16, height: 16, borderWidth: on ? 0 : 2 }}>
                    {on && <Check />}
                  </span>
                  {k}
                </button>
              );
            })}
          </div>
        </div>
      </div>
    );
  }

  function renderStep3() {
    if (loadingRewrite) {
      return (
        <div className="ai-loading">
          <div className="ai-spinner" />
          <div className="ai-loading-title">Preparing suggestions…</div>
          
        </div>
      );
    }
    if (rewriteError) {
      return (
        <div className="ai-error-box">
          <p>{rewriteError}</p>
          <button className="ai-btn ai-btn-soft" onClick={generate}>Try again</button>
        </div>
      );
    }
    if (!rewrite) return null;

    const jobKeywords = analysis ? [...analysis.matched_keywords, ...analysis.missing_keywords] : [];

    if (editing) {
      return (
        <ResumeEditor
          value={editedDoc}
          onChange={setEditedDoc}
          previewRef={previewRef}
          onUndo={undo}
          onRedo={redo}
          canUndo={canUndo}
          canRedo={canRedo}
          keywords={jobKeywords}
          jobId={effJobId}
        />
      );
    }

    const highlight: HighlightState = {
      mode: highlightMode,
      terms: heatmapTerms(analyzeKeywords(jobKeywords, editedDoc)),
      changed: changedStrings(rewrite.original_document, editedDoc),
      figures: rewrite.figures_to_verify ?? [],
    };
    const hasContent = rewrite.document.sections.some(
      (s) => s.items.length > 0 || s.skills.length > 0 || s.text.trim() !== "" || Object.keys(s.groups).length > 0
    );

    const orig = rewrite.original_overall_score;
    const next = rewrite.new_overall_score;
    const bothScored = typeof orig === "number" && typeof next === "number";
    const jumpHeadline =
      bothScored && next > orig
        ? `With these suggestions your fit score goes from ${orig} to ${next}`
        : bothScored && next === orig
          ? `Your fit score holds at ${next}`
          : "Suggestions ready. Accept what sounds like you.";

    const changes: string[] =
      rewrite.changes && rewrite.changes.length
        ? rewrite.changes
        : ["Suggested edits for this role"];
    const gaps = rewrite.gaps ?? [];
    const figures = rewrite.figures_to_verify ?? [];

    return (
      <>
        {!hasContent && (
          <div
            style={{
              background: "#fef3c7",
              border: "1px solid #fde68a",
              color: "#92400e",
              borderRadius: 10,
              padding: "10px 12px",
              fontSize: "0.85rem",
              marginBottom: 12,
            }}
          >
            ⚠️ We couldn't fully read this resume's structure, so the result may be limited. A simpler
            one-column PDF or DOCX usually parses best, or add sections in the editor.
          </div>
        )}
        <div className="ai-review">
          {/* Left: full tailored resume rendered by the single-source renderer */}
          <div className="ai-doc-wrap">
            <FittedResume document={editedDoc} innerRef={previewRef} highlight={highlight} />
          </div>

          {/* Right: score jump + what changed */}
          <aside className="ai-side">
            <div className="ai-side-gauge">
              <Gauge
                score={next}
                label={rewrite.match_label || (typeof next === "number" ? bandFor(next) : "")}
                size="sm"
              />
            </div>
            <div className="ai-side-jump">
              <Spark />
              <p>{jumpHeadline}</p>
            </div>
            <div className="ai-side-stats">
              {rewrite.new_keyword_coverage != null && `${rewrite.new_keyword_coverage}% keyword coverage`}
            </div>
            <button className="ai-btn ai-btn-soft" style={{ width: "100%", justifyContent: "center" }} onClick={() => setEditing(true)}>
              Edit resume
            </button>

            <AtsPanel
              keywords={jobKeywords}
              document={editedDoc}
              suggestions={analysis?.suggestions}
              onAddSkills={applySkills}
              highlightMode={highlightMode}
              onHighlightModeChange={setHighlightMode}
            />

            {effJobId != null && (
              <VersionsPanel
                jobId={effJobId}
                resumeId={resumeId}
                currentDoc={editedDoc}
                originalDoc={rewrite.original_document}
                refreshKey={rewrite.version_id}
                onRestore={(doc) => resetEditedDoc(doc)}
              />
            )}

            <div className="ai-card-label">See what's changed</div>
            <ul className="ai-changes-list">
              {changes.map((c, i) => (
                <li key={i}><span className="ai-change-tick"><Check /></span>{c}</li>
              ))}
            </ul>

            {(gaps.length > 0 || figures.length > 0) && (
              <div className="ai-gaps-card">
                <div className="ai-card-label">Gaps to consider</div>
                {gaps.length > 0 && (
                  <ul className="ai-gaps-list">
                    {gaps.map((g, i) => <li key={i}>{g}</li>)}
                  </ul>
                )}
                {figures.length > 0 && (
                  <div className="ai-gaps-verify">
                    Verify these figures (not in your original): <strong>{figures.join(", ")}</strong>
                  </div>
                )}
              </div>
            )}
          </aside>
        </div>
      </>
    );
  }

  function renderFooter() {
    // A closed posting's link is dead (or lands on a careers home page).
    const applyNow = job.closed
      ? <button className="ai-btn ai-btn-primary" disabled>Posting closed</button>
      : <a className="ai-btn ai-btn-primary" href={job.url} target="_blank" rel="noopener noreferrer">Apply Now</a>;
    if (step === 1) {
      return (
        <div className="ai-modal-foot">
          <button
            className="ai-btn ai-btn-primary"
            disabled={!analysis || loadingAnalysis}
            onClick={() => setStep(2)}
          >
            <Spark /> Suggest Edits for This Job
          </button>
        </div>
      );
    }
    if (step === 2) {
      return (
        <div className="ai-modal-foot">
          <button className="ai-btn ai-btn-ghost ai-foot-left" onClick={() => setStep(1)}>← Back</button>
          <button
            className="ai-btn ai-btn-primary"
            disabled={sections.size === 0}
            onClick={generate}
          >
            <Spark /> Get Suggestions
          </button>
        </div>
      );
    }
    if (editing) {
      return (
        <div className="ai-modal-foot">
          <button className="ai-btn ai-btn-ghost ai-foot-left" onClick={() => setEditing(false)}>← Done editing</button>
          <button className="ai-btn ai-btn-soft" onClick={downloadPdf} disabled={!rewrite}>Download PDF</button>
          <button className="ai-btn ai-btn-soft" onClick={downloadDocxFile} disabled={!rewrite}>Download DOCX</button>
          {onAttach
            ? <button className="ai-btn ai-btn-primary" onClick={() => void onAttach()} disabled={!rewrite}>Attach to application</button>
            : applyNow}
        </div>
      );
    }
    return (
      <div className="ai-modal-foot">
        <button className="ai-btn ai-btn-ghost ai-foot-left" onClick={() => setStep(2)} disabled={loadingRewrite}>← Adjust</button>
        <button className="ai-btn ai-btn-ghost" onClick={generate} disabled={loadingRewrite}>Try Again</button>
        <button className="ai-btn ai-btn-ghost" onClick={copy} disabled={!rewrite}>{copied ? "Copied!" : "Copy"}</button>
        <button className="ai-btn ai-btn-soft" onClick={downloadPdf} disabled={!rewrite}>Download PDF</button>
        <button className="ai-btn ai-btn-soft" onClick={downloadDocxFile} disabled={!rewrite}>Download DOCX</button>
        {onAttach
          ? <button className="ai-btn ai-btn-primary" onClick={() => void onAttach()} disabled={!rewrite}>Attach to application</button>
          : applyNow}
      </div>
    );
  }
}
