import { useEffect } from "react";
import { X } from "@phosphor-icons/react";
import type { PageIntroContent, PageIntroId } from "./pageIntros";
import { PageIntroPreview } from "./PageIntroPreview";
import { Logo } from "../components/brand";
import "./page-intro.css";

interface Props {
  id: PageIntroId;
  content: PageIntroContent;
  onClose: () => void;
}

/** Presentational first-visit intro modal. Themed with our tokens. */
export function PageIntroModal({ id, content, onClose }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="pgi-overlay" onClick={onClose}>
      <div
        className="pgi-card"
        role="dialog"
        aria-modal="true"
        aria-label={content.title}
        onClick={(e) => e.stopPropagation()}
      >
        <button className="pgi-close" onClick={onClose} aria-label="Close">
          <X size={18} weight="bold" />
        </button>

        <div className="pgi-banner">
          <div className="pgi-badge">
            <Logo variant="mark" theme="light" size={34} />
          </div>
          <span className="pgi-eyebrow">{content.eyebrow}</span>
          <h2 className="pgi-title">{content.title}</h2>
          <p className="pgi-desc">{content.description}</p>

          <PageIntroPreview id={id} />
        </div>

        <div className="pgi-footer">
          <button className="pgi-btn" onClick={onClose}>Start Now</button>
        </div>
      </div>
    </div>
  );
}
