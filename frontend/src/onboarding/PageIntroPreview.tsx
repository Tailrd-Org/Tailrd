import type { PageIntroId } from "./pageIntros";
import { Logo } from "../components/brand";

/**
 * A detailed, themed mini-mock of each page/feature, shown inside the intro
 * modal. Purely decorative (aria-hidden), CSS-driven, no real data.
 */
export function PageIntroPreview({ id }: { id: PageIntroId }) {
  return (
    <div className="pgi-mock" aria-hidden>
      <div className="pgi-mock-bar">
        <span className="pgi-mock-brand">
          <Logo variant="mark" theme="light" size={16} decorative />
          <b>Tailrd</b>
        </span>
        <span className="pgi-mock-dots"><i /><i /><i /></span>
      </div>
      <div className="pgi-mock-body">{renderBody(id)}</div>
    </div>
  );
}

function renderBody(id: PageIntroId) {
  switch (id) {
    case "applications":
      return (
        <div className="pgi-apps">
          {[
            { c: "#635bff", t: "Frontend Engineer", s: "Interview", cls: "s-interview" },
            { c: "#0aa678", t: "Product Designer", s: "Offer", cls: "s-offer" },
            { c: "#f2994a", t: "Data Analyst", s: "Applied", cls: "s-applied" },
          ].map((r) => (
            <div className="pgi-app-row" key={r.t}>
              <span className="pgi-app-logo" style={{ background: r.c }} />
              <span className="pgi-app-info">
                <b>{r.t}</b>
                <i />
              </span>
              <span className={`pgi-pill ${r.cls}`}>{r.s}</span>
            </div>
          ))}
        </div>
      );

    case "resume":
      return (
        <div className="pgi-doc">
          <div className="pgi-doc-head">
            <span className="pgi-doc-name" />
            <span className="pgi-badge-tag">Base resume</span>
          </div>
          <span className="pgi-line w90" />
          <span className="pgi-line w70" />
          <div className="pgi-doc-cols">
            <div>
              <span className="pgi-line w60 h" />
              <span className="pgi-line w80" />
              <span className="pgi-line w70" />
              <span className="pgi-line w85" />
            </div>
            <div>
              <span className="pgi-line w50 h" />
              <span className="pgi-line w75" />
              <span className="pgi-line w65" />
              <span className="pgi-line w70" />
            </div>
          </div>
        </div>
      );

    case "interview":
      return (
        <div className="pgi-doc">
          <span className="pgi-pill s-interview" style={{ marginBottom: 10 }}>Question 1</span>
          <span className="pgi-line w85 h" />
          <span className="pgi-line w70" />
          <div className="pgi-answer">
            <span className="pgi-answer-tag">Talking points</span>
            <span className="pgi-line w90" />
            <span className="pgi-line w80" />
          </div>
        </div>
      );

    case "profile":
      return (
        <div className="pgi-profile">
          <div className="pgi-profile-head">
            <span className="pgi-avatar" />
            <span className="pgi-profile-id">
              <span className="pgi-line w70 h" />
              <span className="pgi-line w50" />
            </span>
          </div>
          {["w40", "w40", "w40"].map((_, i) => (
            <div className="pgi-field" key={i}>
              <span className="pgi-line w30 h" />
              <span className="pgi-input" />
            </div>
          ))}
        </div>
      );
  }
}
