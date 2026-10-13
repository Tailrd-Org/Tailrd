/**
 * Pure builders for the overlay's "Generate Custom Resume" result card.
 * Kept DOM-free so the markup is unit-testable; overlay.ts injects the returned
 * HTML and wires the buttons.
 */
import type { TailorResult } from "../shared/types";

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** "Match 7.2 → 8.5" / "Match held at 8.0" from 0-100 scores. The match engine gives no
 * number when it is not confident or cannot score (null): then the label it sent is shown. */
export function scoreJumpText(before: number | null, after: number | null, label = ""): string {
  if (after === null) return label;
  if (before === null) return `Match ${(after / 10).toFixed(1)}`;
  const b = (before / 10).toFixed(1);
  const a = (after / 10).toFixed(1);
  if (after > before) return `Match ${b} → ${a}`;
  if (after === before) return `Match held at ${a}`;
  return `Match ${a}`;
}

/** Inner HTML for the result card. `selected` = keyword chips currently on. */
export function buildTailorCardHtml(result: TailorResult, selected: Set<string>): string {
  const jump = scoreJumpText(result.originalScore, result.newScore, result.matchLabel);
  const stats = [
    result.atsScore !== null ? `ATS ${result.atsScore}` : "",
    result.keywordCoverage !== null ? `${result.keywordCoverage}% coverage` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const chips = result.missingKeywords
    .map(
      (k) =>
        `<button class="ap-kw${selected.has(k) ? " on" : ""}" data-kw="${esc(k)}" type="button">${esc(k)}</button>`
    )
    .join("");
  const kwBlock = result.missingKeywords.length
    ? `<div class="ap-kw-label">Keywords from the posting you could add</div><div class="ap-kw-row">${chips}</div>`
    : "";
  // Single primary action: open a PDF preview. Regenerate / Download / Attach
  // live INSIDE the preview (see overlay's PDF modal), so the card stays minimal.
  const eye =
    '<svg viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M247.31,124.76c-.35-.79-8.82-19.58-27.65-38.41C194.57,61.26,162.88,48,128,48S61.43,61.26,36.34,86.35C17.51,105.18,9,124,8.69,124.76a8,8,0,0,0,0,6.5c.35.79,8.82,19.57,27.65,38.4C61.43,194.74,93.12,208,128,208s66.57-13.26,91.66-38.34c18.83-18.83,27.3-37.61,27.65-38.4A8,8,0,0,0,247.31,124.76ZM128,168a40,40,0,1,1,40-40A40,40,0,0,1,128,168Z"/></svg>';
  return (
    `<div class="ap-tailor-scores"><span class="ap-tailor-jump">${esc(jump)}</span>` +
    `<span class="ap-tailor-stats">${esc(stats)}</span></div>` +
    kwBlock +
    `<div class="ap-tailor-actions">` +
    `<button class="ap-btn-upload ap-btn-icon" id="ap-tailor-preview" type="button">${eye}Preview résumé</button>` +
    `</div>` +
    `<div class="ap-upload-status" id="ap-tailor-status"></div>`
  );
}
