import { WORDMARK_PATH } from "./logo.constants";

interface WordmarkGraphicProps {
  /** Offset of the T's top-left corner, in mark units. */
  x?: number;
  y?: number;
  /** Fades in alongside the draw-on intro. */
  animate?: boolean;
}

/**
 * "Tailrd" as outlines. The lettering is custom (no typeface matches it), so it
 * ships as paths rather than <text>: identical everywhere, no font to load.
 */
export function WordmarkGraphic({ x = 0, y = 0, animate = false }: WordmarkGraphicProps) {
  return (
    <path
      d={WORDMARK_PATH}
      transform={x || y ? `translate(${x} ${y})` : undefined}
      className={animate ? "tailrd-logo__fade" : undefined}
      style={{ fill: "var(--tailrd-logo-ink, #09090B)" }}
    />
  );
}
