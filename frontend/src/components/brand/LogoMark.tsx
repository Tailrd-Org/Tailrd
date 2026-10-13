import type { CSSProperties } from "react";
import { APP_ICON, BRAND_COLORS, MARK } from "./logo.constants";

// The fallbacks keep the mark coloured even if brand.css has not loaded.
const MARK_COLOR = "var(--tailrd-logo-mark, #6247E5)";

interface MarkGraphicProps {
  ring: number;
  detail: number;
  /** Adds the draw-on intro classes (see brand.css). */
  animate?: boolean;
  /** Extra class on each trail dash; the spinner pulses them. */
  dashClassName?: string;
  dashStyle?: (index: number) => CSSProperties | undefined;
}

/** The outlined mark in its 48-unit box: ring, plane silhouette, folds, trail. */
export function MarkGraphic({ ring, detail, animate = false, dashClassName, dashStyle }: MarkGraphicProps) {
  // pathLength normalises every stroke to 1 so one dash animation fits them all.
  const len = animate ? 1 : undefined;
  const late = animate ? "tailrd-logo__draw tailrd-logo__draw--late" : undefined;
  return (
    <g fill="none" strokeLinecap="round" strokeLinejoin="round" style={{ stroke: MARK_COLOR }}>
      <path d={MARK.ring} strokeWidth={ring} className={animate ? "tailrd-logo__draw" : undefined} pathLength={len} />
      <g strokeWidth={detail}>
        <path d={MARK.plane} className={late} pathLength={len} />
        {MARK.folds.map((d) => (
          <path key={d} d={d} className={late} pathLength={len} />
        ))}
        {MARK.trail.map((d, i) => (
          <path
            key={d}
            d={d}
            className={[late, dashClassName].filter(Boolean).join(" ") || undefined}
            style={dashStyle?.(i)}
            pathLength={len}
          />
        ))}
      </g>
    </g>
  );
}

/** The app icon: white plane and trail on solid primary. Fixed colours in every theme. */
export function AppIconGraphic() {
  return (
    <>
      <rect width="48" height="48" rx={APP_ICON.radius} fill={BRAND_COLORS.primary} />
      <g
        transform={APP_ICON.transform}
        fill="none"
        stroke="#FFFFFF"
        strokeWidth={APP_ICON.stroke}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={MARK.plane} />
        {MARK.folds.map((d) => (
          <path key={d} d={d} />
        ))}
        {MARK.trail.map((d) => (
          <path key={d} d={d} />
        ))}
      </g>
    </>
  );
}
