/**
 * Tailrd brand constants: colours, mark geometry, lettering and lockup metrics.
 *
 * The geometry is a least-squares vector fit to the existing artwork
 * (docs/Logo.jpeg and frontend/public/icon-512.png, IoU 0.94 for the mark and
 * 0.99 for the lettering), normalised to a 48 x 48 box with the ring centred at
 * (24, 24), r = 21.5. brand/README.md has the provenance and usage rules.
 *
 * These strings are the single source of truth: the React components render
 * them, scripts/brand/build-brand.mjs writes every standalone SVG and PNG from
 * them, and src/__tests__/brand-logo.test.tsx pins them so a change is always
 * deliberate. Keep this file free of imports so the build script can load it.
 */

export const BRAND_COLORS = {
  /** The mark on light backgrounds. */
  primary: "#6247E5",
  /** The mark on dark backgrounds, lightened for contrast (5.79:1 on surfaceDark). */
  primaryDarkMode: "#8B7BF0",
  /** The wordmark on light backgrounds. */
  ink: "#09090B",
  /** The wordmark on dark backgrounds. */
  inkInverse: "#FAFAFA",
  surface: "#FFFFFF",
  surfaceDark: "#0B0B10",
} as const;

export type LogoVariant = "horizontal" | "stacked" | "mark" | "wordmark" | "appIcon";
export type LogoTheme = "auto" | "light" | "dark" | "mono-black" | "mono-white" | "currentColor";
export type ExplicitLogoTheme = Exclude<LogoTheme, "auto">;

/** Mark and wordmark colours per theme. "auto" is resolved in brand.css by prefers-color-scheme. */
export const THEME_COLORS: Record<ExplicitLogoTheme, { mark: string; ink: string }> = {
  light: { mark: BRAND_COLORS.primary, ink: BRAND_COLORS.ink },
  dark: { mark: BRAND_COLORS.primaryDarkMode, ink: BRAND_COLORS.inkInverse },
  "mono-black": { mark: BRAND_COLORS.ink, ink: BRAND_COLORS.ink },
  "mono-white": { mark: "#FFFFFF", ink: "#FFFFFF" },
  currentColor: { mark: "currentColor", ink: "currentColor" },
};

/**
 * Inline CSS custom properties for an explicit theme. "auto" returns nothing so
 * brand.css can resolve it by prefers-color-scheme.
 */
export function themeVars(theme: LogoTheme): Record<string, string> | undefined {
  if (theme === "auto") return undefined;
  const c = THEME_COLORS[theme];
  return { "--tailrd-logo-mark": c.mark, "--tailrd-logo-ink": c.ink };
}

/**
 * The mark: outlined strokes with round caps and joins, in a 48-unit box.
 * Drawing order is ring, plane, folds, trail (the draw-on intro follows it).
 */
export const MARK = {
  viewBox: "0 0 48 48",
  /** Outer ring about (24, 24), r = 21.5, broken by a 31.9 deg gap at the lower left. */
  ring: "M4.92 33.91A21.5 21.5 0 1 1 13.05 42.5",
  /** Plane silhouette: wing tip, nose, tail tip, keel junction, keel tip, wing root. */
  plane: "M9.61 22.65L37.97 12.83L29.99 35.94L23.48 31.59L18.23 36.08L16.41 27.5Z",
  /** Folds: the wing's inner edge, the centre crease running down the keel, the tail panel's lower edge. */
  folds: ["M16.41 27.5L37.97 12.83", "M37.97 12.83L20.41 29.53L18.23 36.08", "M20.41 29.53L23.48 31.59"],
  /**
   * Four dashes on two parallel speed lines at 37 deg, left to right by position:
   * the outer pair exits through the ring's gap.
   */
  trail: ["M8.34 36.17L5.69 38.17", "M10.01 39.72L5.96 42.78", "M13.44 32.33L10.53 34.52", "M15.06 35.91L12.01 38.22"],
  /** Measured weights: the ring is about 57% heavier than the plane and trail. */
  ringStroke: 1.73,
  detailStroke: 1.1,
} as const;

/**
 * Optical compensation by rendered size (px). The source weights are the
 * large-size tier. Below it, each tier keeps the plane's lines at roughly one
 * device pixel or more (and the ring about 1.5x that): thinner strokes
 * antialias into a washed-out, grainy line on 1x screens.
 */
export const STROKE_RAMP: ReadonlyArray<{ minSize: number; ring: number; detail: number }> = [
  { minSize: 64, ring: 1.73, detail: 1.1 },
  { minSize: 48, ring: 2.1, detail: 1.35 },
  { minSize: 32, ring: 2.6, detail: 1.75 },
  { minSize: 24, ring: 2.8, detail: 1.9 },
  { minSize: 20, ring: 3.2, detail: 2.2 },
  { minSize: 0, ring: 3.6, detail: 2.6 },
];

export function strokesFor(size: number): { ring: number; detail: number } {
  const tier = STROKE_RAMP.find((t) => size >= t.minSize) ?? STROKE_RAMP[STROKE_RAMP.length - 1];
  return { ring: tier.ring, detail: tier.detail };
}

/**
 * The lettering, rebuilt from the source as geometric outlines (no typeface
 * matches it). Local units: origin at the T's top-left, cap height 21.62.
 */
export const WORDMARK = {
  width: 75.34,
  capHeight: 21.62,
  glyphs: {
    T: "M0 0L18.93 0L18.93 3.64L11.27 3.64L11.27 21.62L7.52 21.62L7.52 3.64L0 3.64Z",
    a:
      "M18.76 5.85L27.87 5.85A5.22 5.22 0 0 1 33.09 11.06L33.09 16.4A5.22 5.22 0 0 1 27.87 21.62L21.24 21.62" +
      "A4.9 4.9 0 0 1 16.34 16.72L16.34 16.3A4.36 4.36 0 0 1 20.7 11.94L29.41 11.94L29.41 11.06" +
      "A1.86 1.86 0 0 0 27.55 9.2L18.76 9.2Z" +
      "M29.41 16.78A1.55 1.55 0 0 0 27.86 15.22L21.55 15.22A1.55 1.55 0 0 0 19.99 16.78" +
      "A1.55 1.55 0 0 0 21.55 18.33L27.86 18.33A1.55 1.55 0 0 0 29.41 16.78Z",
    i: "M35.78 0L39.45 0L39.45 3.77L35.78 3.77ZM35.78 5.85L39.45 5.85L39.45 21.62L35.78 21.62Z",
    l: "M42.83 0L46.51 0L46.51 21.62L42.83 21.62Z",
    r: "M49.72 21.62L49.72 10.41A4.56 4.56 0 0 1 54.28 5.85L58.68 5.85L58.68 9.13L55.25 9.13A1.86 1.86 0 0 0 53.39 11L53.39 21.62Z",
    d:
      "M71.67 0L75.34 0L75.34 16.53A5.08 5.08 0 0 1 70.26 21.62L65.99 21.62A6.96 6.96 0 0 1 59.03 14.65" +
      "L59.03 12.81A6.96 6.96 0 0 1 65.99 5.85L71.67 5.85Z" +
      "M71.67 9.13L66.46 9.13A3.67 3.67 0 0 0 62.79 12.81L62.79 14.65A3.67 3.67 0 0 0 66.46 18.33" +
      "L69.52 18.33A2.15 2.15 0 0 0 71.67 16.18Z",
  },
} as const;

/** All glyphs as one path, T a i l r d. */
export const WORDMARK_PATH = Object.values(WORDMARK.glyphs).join("");

/**
 * Lockup layouts in mark units (the icon box is 48 x 48). The margin mirrors
 * the ring's own inset inside its box, so every lockup has even internal padding.
 */
export const LOCKUP = {
  margin: 1.64,
  /** Mark at the origin; gap 3.97 (0.083 S, measured); cap-height centre level with the icon centre. */
  horizontal: { width: 128.95, height: 48, wordmarkX: 51.97, wordmarkY: 13.19 },
  /** Mark centred over the wordmark; cap top 0.18 S below the icon box (spec section 4). */
  stacked: { width: 78.62, height: 79.9, markX: 15.31, wordmarkX: 1.64, wordmarkY: 56.64 },
  wordmark: { width: 75.34, height: 21.62 },
} as const;

/**
 * The browser favicon and extension toolbar icons are the real mark on a
 * transparent ground (no tile), cropped to the ring so it fills the icon, with
 * strokes hinted per pixel size.
 */
export const ICON_STROKES: Readonly<Record<number, { ring: number; detail: number }>> = {
  16: { ring: 3.6, detail: 2.6 },
  32: { ring: 2.8, detail: 1.9 },
  48: { ring: 2.1, detail: 1.35 },
  128: { ring: 1.73, detail: 1.1 },
};

/** viewBox cropped to the ring's outer edge plus a hair of padding, for a given ring stroke. */
export function iconViewBox(ring: number): string {
  const pad = ring / 2 + 0.2;
  const o = Math.round((2.5 - pad) * 100) / 100;
  const s = Math.round((43 + 2 * pad) * 100) / 100;
  return `${o} ${o} ${s} ${s}`;
}

/**
 * App icon (spec section 5): white plane and trail on solid primary, no ring.
 * The ring's footprint sets the scale (64% of the canvas); the plane and trail
 * are then optically centred. The stroke is the spec's heavier 2.6-of-1.5 applied
 * to the measured detail weight (1.1 x 2.6 / 1.5); a flat 2.6 clogs the keel.
 */
export const APP_ICON = {
  /** Apple's continuous-corner ratio, 22.37% of the canvas. */
  radius: 10.74,
  transform: "translate(9.01 4.9) scale(0.6868)",
  stroke: 1.91,
} as const;
