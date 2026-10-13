import { useState } from "react";

const API_BASE = "";

interface Props {
  jobId: number;
}

// No fit tool: /ai/analyze-fit is gone. Fit comes from the deterministic engine and
// JobDetailView shows it (POST /ai/match-breakdown).
type ToolType = "resume" | "cover-letter";

interface ToolState {
  loading: boolean;
  content: string;
  error: string;
}

export default function AIToolsSidebar({ jobId }: Props) {
  const [tools, setTools] = useState<Record<ToolType, ToolState>>({
    resume: { loading: false, content: "", error: "" },
    "cover-letter": { loading: false, content: "", error: "" },
  });
  const [copied, setCopied] = useState<ToolType | null>(null);

  async function runTool(type: ToolType) {
    setTools((prev) => ({
      ...prev,
      [type]: { loading: true, content: "", error: "" },
    }));

    const endpoints: Record<ToolType, string> = {
      resume: `${API_BASE}/ai/tailor-resume/${jobId}`,
      "cover-letter": `${API_BASE}/ai/cover-letter/${jobId}`,
    };

    try {
      const res = await fetch(endpoints[type], { method: "POST" });
      if (!res.ok) {
        if (res.status === 503) {
          setTools((prev) => ({
            ...prev,
            [type]: { loading: false, content: "", error: "Ollama is not running. Please start Ollama to enable AI features." },
          }));
          return;
        }
        setTools((prev) => ({
          ...prev,
          [type]: { loading: false, content: "", error: "Failed to generate content. Please try again." },
        }));
        return;
      }
      const data = await res.json();
      const content = data.tailored_text || data.cover_letter_text || data.analysis || data.content || JSON.stringify(data);
      setTools((prev) => ({
        ...prev,
        [type]: { loading: false, content, error: "" },
      }));
    } catch {
      setTools((prev) => ({
        ...prev,
        [type]: { loading: false, content: "", error: "Failed to connect to the server." },
      }));
    }
  }

  function handleCopy(type: ToolType) {
    navigator.clipboard.writeText(tools[type].content);
    setCopied(type);
    setTimeout(() => setCopied(null), 2000);
  }

  function handleDownload(type: ToolType) {
    const blob = new Blob([tools[type].content], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${type}-${jobId}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const toolButtons: { type: ToolType; label: string; icon: string }[] = [
    { type: "resume", label: "Customize Your Resume", icon: "📄" },
    { type: "cover-letter", label: "Build Cover Letter", icon: "✉️" },
  ];

  return (
    <div className="ai-tools-sidebar">
      <h3>AI Tools</h3>
      <div className="ai-tools-buttons">
        {toolButtons.map(({ type, label, icon }) => (
          <button
            key={type}
            className="ai-tool-btn"
            data-tour={
              type === "resume" ? "ai-tool-resume"
              : "ai-tool-cover-letter"
            }
            onClick={() => runTool(type)}
            disabled={tools[type].loading}
          >
            <span className="ai-tool-icon">{icon}</span>
            <span>{label}</span>
          </button>
        ))}
      </div>

      {toolButtons.map(({ type }) => {
        const state = tools[type];
        if (!state.loading && !state.content && !state.error) return null;

        return (
          <div key={type} className="ai-tool-result">
            {state.loading && (
              <div className="ai-tool-loading">
                <div className="spinner" />
                <span>Generating... (est. 15-30s)</span>
              </div>
            )}

            {state.error && (
              <div className="ai-tool-error">{state.error}</div>
            )}

            {state.content && (
              <div className="ai-tool-content">
                <pre className="ai-tool-text">{state.content}</pre>
                <div className="ai-tool-actions">
                  <button className="btn-sm" onClick={() => handleCopy(type)}>
                    {copied === type ? "Copied!" : "Copy"}
                  </button>
                  <button className="btn-sm" onClick={() => handleDownload(type)}>
                    Download
                  </button>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
