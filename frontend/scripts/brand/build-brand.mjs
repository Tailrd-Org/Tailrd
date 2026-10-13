/**
 * Regenerate every brand asset from src/components/brand/logo.constants.ts.
 *
 *   npm run brand                          # SVGs, PNGs, favicon.ico, manifest, OG image, preview
 *   npm run brand -- --svg-only            # just the SVGs (no browser needed)
 *   npm run brand -- --og-title "Pricing"  # also writes brand/og/og-pricing.png with a page title
 *
 * Writes:
 *   src/assets/brand/*.svg   standalone SVGs: role="img" + <title>, attributes only, no width/height
 *   public/                  favicon.svg (adaptive), favicon.ico, PNG icons, og-image.png, site.webmanifest
 *   brand/                   handoff PNGs (@1x/@2x/@3x), email signature, social avatar, preview.html
 *   ../chrome-extension/     assets/icon-{16,32,48,128}.png and src/content/brandLogo.ts (inline SVG)
 *
 * PNGs are rendered by Chromium through the Playwright install that ships with
 * chrome-extension/ (run `npm install` there first if it is missing).
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  APP_ICON,
  BRAND_COLORS,
  ICON_STROKES,
  LOCKUP,
  MARK,
  STROKE_RAMP,
  THEME_COLORS,
  WORDMARK,
  iconViewBox,
  strokesFor,
} from "../../src/components/brand/logo.constants.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(here, "..", "..");
const REPO = path.resolve(FRONTEND, "..");
const OUT_SVG = path.join(FRONTEND, "src", "assets", "brand");
const OUT_PUBLIC = path.join(FRONTEND, "public");
const OUT_BRAND = path.join(FRONTEND, "brand");
const EXT = path.join(REPO, "chrome-extension");

const args = process.argv.slice(2);
const svgOnly = args.includes("--svg-only");
const ogTitleIdx = args.indexOf("--og-title");
const ogTitle = ogTitleIdx >= 0 ? args[ogTitleIdx + 1] : null;

// ---------------------------------------------------------------- SVG builders

const THEMES = ["light", "dark", "mono-black", "mono-white"];
const indent = (lines, pad) => lines.map((l) => (l ? pad + l : l)).join("\n");

function markLines(color, { ring = MARK.ringStroke, detail = MARK.detailStroke } = {}) {
  return [
    `<g fill="none" stroke="${color}" stroke-linecap="round" stroke-linejoin="round">`,
    `  <!-- 1. Ring: r = 21.5 about (24, 24), broken by a 31.9 deg gap at the lower`,
    `       left where the trail exits. Heavier than the plane, as in the source art. -->`,
    `  <path stroke-width="${ring}" d="${MARK.ring}"/>`,
    `  <g stroke-width="${detail}">`,
    `    <!-- 2. Plane silhouette: wing tip, nose, tail tip, keel junction, keel tip, wing root. -->`,
    `    <path d="${MARK.plane}"/>`,
    `    <!-- 3. Folds: the wing's inner edge, the centre crease running down the keel,`,
    `         and the tail panel's lower edge. -->`,
    ...MARK.folds.map((d) => `    <path d="${d}"/>`),
    `    <!-- 4. Trail: four dashes on two parallel speed lines at 37 deg; the outer`,
    `         pair exits through the gap in the ring. -->`,
    ...MARK.trail.map((d) => `    <path d="${d}"/>`),
    `  </g>`,
    `</g>`,
  ];
}

function wordmarkLines(color, x = 0, y = 0) {
  const move = x || y ? ` transform="translate(${x} ${y})"` : "";
  return [
    `<!-- Wordmark: the custom "Tailrd" lettering as outlines, so it renders without any font. -->`,
    `<g fill="${color}"${move}>`,
    ...Object.entries(WORDMARK.glyphs).flatMap(([ch, d]) => [`  <!-- ${ch} -->`, `  <path d="${d}"/>`]),
    `</g>`,
  ];
}

function svgDoc(viewBox, bodyLines, comment) {
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}" role="img">`,
    `  <title>Tailrd</title>`,
    comment ? `  <!-- ${comment} -->` : null,
    indent(bodyLines, "  "),
    `</svg>`,
    ``,
  ]
    .filter((l) => l !== null)
    .join("\n");
}

function lockupSvg(variant, theme, strokes) {
  const { mark, ink } = THEME_COLORS[theme];
  const H = LOCKUP.horizontal;
  const S = LOCKUP.stacked;
  switch (variant) {
    case "mark":
      return svgDoc(MARK.viewBox, markLines(mark, strokes), `Tailrd mark, ${theme}. 48-unit box; round caps and joins throughout.`);
    case "wordmark":
      return svgDoc(`0 0 ${LOCKUP.wordmark.width} ${LOCKUP.wordmark.height}`, wordmarkLines(ink), `Tailrd wordmark, ${theme}.`);
    case "horizontal":
      return svgDoc(
        `0 0 ${H.width} ${H.height}`,
        [`<!-- Mark: the 48 x 48 icon box at the origin. -->`, ...markLines(mark, strokes), ``, ...wordmarkLines(ink, H.wordmarkX, H.wordmarkY)],
        `Tailrd horizontal lockup, ${theme}. Gap 0.083 S; cap-height centre level with the icon centre.`,
      );
    case "stacked":
      return svgDoc(
        `0 0 ${S.width} ${S.height}`,
        [
          `<!-- Mark: centred over the wordmark. -->`,
          `<g transform="translate(${S.markX} 0)">`,
          ...markLines(mark, strokes).map((l) => "  " + l),
          `</g>`,
          ``,
          ...wordmarkLines(ink, S.wordmarkX, S.wordmarkY),
        ],
        `Tailrd stacked lockup, ${theme}. Cap top sits 0.18 S below the icon box.`,
      );
    default:
      throw new Error(`unknown variant ${variant}`);
  }
}

function appIconSvg({ fullBleed = false } = {}) {
  const tile = fullBleed
    ? `<rect width="48" height="48" fill="${BRAND_COLORS.primary}"/>`
    : `<rect width="48" height="48" rx="${APP_ICON.radius}" fill="${BRAND_COLORS.primary}"/>`;
  return svgDoc(
    MARK.viewBox,
    [
      fullBleed
        ? `<!-- Full-bleed tile, for platforms that apply their own mask (iOS, Android maskable). -->`
        : `<!-- Rounded tile: corner radius 22.37% of the canvas. -->`,
      tile,
      `<!-- Plane and trail in white, no ring: the tile already contains the mark. The ring's`,
      `     footprint sets the scale (64% of the canvas), then the plane is optically centred. -->`,
      `<g transform="${APP_ICON.transform}" fill="none" stroke="#FFFFFF" stroke-width="${APP_ICON.stroke}" stroke-linecap="round" stroke-linejoin="round">`,
      `  <path d="${MARK.plane}"/>`,
      ...MARK.folds.map((d) => `  <path d="${d}"/>`),
      ...MARK.trail.map((d) => `  <path d="${d}"/>`),
      `</g>`,
    ],
    `Tailrd app icon${fullBleed ? ", full bleed" : ""}.`,
  );
}

/**
 * The real mark as an icon: no tile, transparent ground, viewBox cropped to the ring,
 * strokes hinted for the pixel size. `adaptive` (public/favicon.svg, the one SVG
 * allowed a <style>) lightens the mark in dark mode so it reads on dark tab strips.
 */
function iconSvg(px, { adaptive = false } = {}) {
  const strokes = ICON_STROKES[px] ?? ICON_STROKES[32];
  let lines = markLines(BRAND_COLORS.primary, strokes);
  if (adaptive) {
    lines = [
      `<style>`,
      `  .mark { stroke: ${BRAND_COLORS.primary}; }`,
      `  @media (prefers-color-scheme: dark) { .mark { stroke: ${BRAND_COLORS.primaryDarkMode}; } }`,
      `</style>`,
      ...lines.map((l, i) => (i === 0 ? l.replace(`stroke="${BRAND_COLORS.primary}"`, 'class="mark"') : l)),
    ];
  }
  return svgDoc(iconViewBox(strokes.ring), lines, `Tailrd icon, hinted for ${px}px${adaptive ? "; lighter in dark mode" : ""}.`);
}

/** One-line SVG with the comments and <title> stripped, for inlining in another document. */
const compact = (svg) =>
  svg
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<title>[^<]*<\/title>/, "")
    .replace(/\s+/g, " ")
    .replace(/>\s+</g, "><")
    .trim();

// ------------------------------------------------------------------ write SVGs

mkdirSync(OUT_SVG, { recursive: true });
mkdirSync(OUT_BRAND, { recursive: true });
const svgFiles = {
  "tailrd-logo-horizontal.svg": lockupSvg("horizontal", "light"),
  "tailrd-logo-horizontal-dark.svg": lockupSvg("horizontal", "dark"),
  "tailrd-logo-horizontal-mono-black.svg": lockupSvg("horizontal", "mono-black"),
  "tailrd-logo-horizontal-mono-white.svg": lockupSvg("horizontal", "mono-white"),
  "tailrd-logo-stacked.svg": lockupSvg("stacked", "light"),
  "tailrd-logo-stacked-dark.svg": lockupSvg("stacked", "dark"),
  "tailrd-mark.svg": lockupSvg("mark", "light"),
  "tailrd-mark-dark.svg": lockupSvg("mark", "dark"),
  "tailrd-mark-mono-black.svg": lockupSvg("mark", "mono-black"),
  "tailrd-mark-mono-white.svg": lockupSvg("mark", "mono-white"),
  "tailrd-wordmark.svg": lockupSvg("wordmark", "light"),
  "tailrd-wordmark-dark.svg": lockupSvg("wordmark", "dark"),
  "tailrd-app-icon.svg": appIconSvg(),
};
for (const [name, svg] of Object.entries(svgFiles)) writeFileSync(path.join(OUT_SVG, name), svg);
writeFileSync(path.join(OUT_BRAND, "tailrd-app-icon-full-bleed.svg"), appIconSvg({ fullBleed: true }));
writeFileSync(path.join(OUT_PUBLIC, "favicon.svg"), iconSvg(16, { adaptive: true }));

// The extension panel inlines the logo as SVG in its shadow root: crisp at any
// device pixel ratio, and unaffected by a host page's img-src CSP.
const extLockup = compact(lockupSvg("horizontal", "light", strokesFor(26))).replace(
  '<svg xmlns="http://www.w3.org/2000/svg"',
  '<svg xmlns="http://www.w3.org/2000/svg" class="ap-brand-lockup" aria-label="Tailrd"',
);
const extMark = compact(lockupSvg("mark", "light", strokesFor(28)))
  .replace('<svg xmlns="http://www.w3.org/2000/svg"', '<svg xmlns="http://www.w3.org/2000/svg" class="ap-edge-mark" aria-hidden="true"')
  .replace(' role="img"', "");
writeFileSync(
  path.join(EXT, "src", "content", "brandLogo.ts"),
  `// AUTO-GENERATED from frontend/src/components/brand/logo.constants.ts by
// frontend/scripts/brand/build-brand.mjs; do not hand-edit.

/** Horizontal lockup for the panel header (strokes hinted for its 26px height). */
export const BRAND_LOCKUP_SVG =
  ${JSON.stringify(extLockup)};

/** The mark alone, for the collapsed-state edge tab (strokes hinted for 28px). */
export const BRAND_MARK_SVG =
  ${JSON.stringify(extMark)};
`,
);
console.log(`wrote ${Object.keys(svgFiles).length} SVGs to src/assets/brand, favicon.svg, brand/tailrd-app-icon-full-bleed.svg`);

// ------------------------------------------------------------------- manifest

writeFileSync(
  path.join(OUT_PUBLIC, "site.webmanifest"),
  JSON.stringify(
    {
      name: "Tailrd",
      short_name: "Tailrd",
      start_url: "/",
      icons: [
        { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
        { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
        { src: "/icon-512-maskable.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
      ],
      theme_color: BRAND_COLORS.primary,
      background_color: BRAND_COLORS.surface,
      display: "standalone",
    },
    null,
    2,
  ) + "\n",
);

// --------------------------------------------------------------- preview page

const ramp = [16, 20, 24, 32, 48, 64, 128];
function rampSvg(size) {
  return lockupSvg("mark", "light", strokesFor(size)).replace('role="img"', `width="${size}" height="${size}" role="img"`);
}
const cell = (svg, bg, w) =>
  `<figure style="background:${bg}"><div style="width:${w}px">${svg.replace('role="img"', 'width="100%" role="img"')}</div></figure>`;
const preview = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tailrd brand preview</title>
<style>
  body { margin: 0; padding: 24px; font: 14px/1.4 system-ui, sans-serif; color: #09090b; background: #f4f4f5; }
  h2 { margin: 32px 0 12px; font-size: 15px; }
  .row { display: flex; flex-wrap: wrap; gap: 12px; }
  figure { margin: 0; padding: 20px; border-radius: 10px; display: flex; align-items: center; justify-content: center; min-width: 120px; }
  .ramp figure { min-width: 0; }
</style>
</head>
<body>
<p>Generated by <code>frontend/scripts/brand/build-brand.mjs</code>. Every variant and theme on light and dark grounds, plus the size ramp as the React component renders it.</p>
${["horizontal", "stacked", "mark", "wordmark"]
  .map(
    (v) => `<h2>${v}</h2>
<div class="row">
${THEMES.map((t) => cell(lockupSvg(v, t), t === "dark" || t === "mono-white" ? BRAND_COLORS.surfaceDark : BRAND_COLORS.surface, v === "horizontal" ? 220 : v === "wordmark" ? 160 : 110)).join("\n")}
${cell(lockupSvg(v, "mono-white"), BRAND_COLORS.primary, v === "horizontal" ? 220 : v === "wordmark" ? 160 : 110)}
</div>`,
  )
  .join("\n")}
<h2>app icon and favicon</h2>
<div class="row">
${cell(appIconSvg(), BRAND_COLORS.surface, 110)}
${cell(appIconSvg({ fullBleed: true }), BRAND_COLORS.surface, 110)}
${cell(iconSvg(48), BRAND_COLORS.surface, 110)}
${cell(iconSvg(48), BRAND_COLORS.surfaceDark, 110)}
</div>
<h2>size ramp (stroke tiers: ${STROKE_RAMP.map((t) => `${t.minSize}px+ ${t.ring}/${t.detail}`).join(", ")})</h2>
<div class="row ramp">
${ramp.map((s) => `<figure style="background:#fff"><div>${rampSvg(s)}<div style="text-align:center;font-size:11px;margin-top:6px">${s}px</div></div></figure>`).join("\n")}
</div>
</body>
</html>
`;
writeFileSync(path.join(OUT_BRAND, "preview.html"), preview);

if (svgOnly) {
  console.log("--svg-only: skipped PNG rendering");
  process.exit(0);
}

// ------------------------------------------------------------------ rasterise

function loadPlaywright() {
  for (const base of [FRONTEND, path.join(REPO, "chrome-extension")]) {
    try {
      return createRequire(path.join(base, "package.json"))("playwright");
    } catch {
      /* try the next install */
    }
  }
  throw new Error("Playwright not found. Run `npm install` in chrome-extension/ (or pass --svg-only).");
}

const { chromium } = loadPlaywright();
const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: 1 });

/** Render an SVG string to a PNG buffer at exactly width x height px. */
async function png(svg, width, height, { background = null } = {}) {
  await page.setViewportSize({ width, height });
  const sized = svg.replace('role="img"', 'width="100%" height="100%" role="img"');
  await page.setContent(
    `<!doctype html><html><body style="margin:0;width:${width}px;height:${height}px;overflow:hidden;background:${background ?? "transparent"}">${sized}</body></html>`,
  );
  return page.screenshot({ type: "png", omitBackground: background === null, clip: { x: 0, y: 0, width, height } });
}

const size = (variant, S) => {
  const box = { horizontal: LOCKUP.horizontal, stacked: LOCKUP.stacked, mark: { width: 48, height: 48 }, wordmark: LOCKUP.wordmark }[variant];
  return [Math.round((box.width / 48) * S), Math.round((box.height / 48) * S)];
};

// public/ icons
const appRounded = appIconSvg();
const appBleed = appIconSvg({ fullBleed: true });
const icoSizes = [16, 32, 48];
const icoPngs = [];
// Each frame is the real mark with strokes hinted for that size.
for (const s of icoSizes) icoPngs.push(await png(iconSvg(s), s, s));
writeFileSync(path.join(OUT_PUBLIC, "favicon.ico"), ico(icoSizes, icoPngs));
writeFileSync(path.join(OUT_PUBLIC, "favicon-96x96.png"), await png(iconSvg(128), 96, 96));
writeFileSync(path.join(OUT_PUBLIC, "apple-touch-icon.png"), await png(appBleed, 180, 180, { background: BRAND_COLORS.primary }));
writeFileSync(path.join(OUT_PUBLIC, "icon-192.png"), await png(appRounded, 192, 192));
writeFileSync(path.join(OUT_PUBLIC, "icon-512.png"), await png(appRounded, 512, 512));
writeFileSync(path.join(OUT_PUBLIC, "icon-512-maskable.png"), await png(appBleed, 512, 512, { background: BRAND_COLORS.primary }));
writeFileSync(path.join(OUT_PUBLIC, "og-image.png"), await ogImage(null));
if (ogTitle) {
  mkdirSync(path.join(OUT_BRAND, "og"), { recursive: true });
  const slug = ogTitle.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  writeFileSync(path.join(OUT_BRAND, "og", `og-${slug}.png`), await ogImage(ogTitle));
}
console.log("wrote public/ favicon.ico, favicon-96x96.png, apple-touch-icon.png, icon-192/512(-maskable).png, og-image.png");

// chrome-extension toolbar icons: the real mark, strokes hinted per size
for (const s of [16, 32, 48, 128]) writeFileSync(path.join(EXT, "assets", `icon-${s}.png`), await png(iconSvg(s), s, s));
console.log("wrote chrome-extension/assets/icon-{16,32,48,128}.png and src/content/brandLogo.ts");

// brand/ handoff PNGs: every lockup x theme at @1x (64px icon) / @2x / @3x
mkdirSync(path.join(OUT_BRAND, "png"), { recursive: true });
let count = 0;
for (const v of ["horizontal", "stacked", "mark", "wordmark"]) {
  for (const t of THEMES) {
    for (const k of [1, 2, 3]) {
      const S = 64 * k;
      const [w, h] = size(v, S);
      const strokes = v === "wordmark" ? undefined : strokesFor(64);
      writeFileSync(path.join(OUT_BRAND, "png", `tailrd-logo-${v}-${t}@${k}x.png`), await png(lockupSvg(v, t, strokes), w, h));
      count++;
    }
  }
}
for (const s of [180, 192, 512, 1024]) {
  writeFileSync(path.join(OUT_BRAND, "png", `tailrd-app-icon-${s}.png`), await png(appRounded, s, s));
  count++;
}
writeFileSync(path.join(OUT_BRAND, "png", "tailrd-app-icon-full-bleed-1024.png"), await png(appBleed, 1024, 1024, { background: BRAND_COLORS.primary }));
const [ew, eh] = [320, Math.round((320 * LOCKUP.horizontal.height) / LOCKUP.horizontal.width)];
writeFileSync(path.join(OUT_BRAND, "email-signature@2x.png"), await png(lockupSvg("horizontal", "light"), ew, eh, { background: "#FFFFFF" }));
writeFileSync(path.join(OUT_BRAND, "social-avatar-400.png"), await png(appBleed, 400, 400, { background: BRAND_COLORS.primary }));
console.log(`wrote ${count + 1} PNGs to brand/png, email-signature@2x.png, social-avatar-400.png`);

await browser.close();

// ------------------------------------------------------------------- helpers

/** OG card: 1200 x 630, dark ground, stacked lockup centred, optional page title in Inter. */
async function ogImage(title) {
  const S = 150;
  const [w, h] = size("stacked", S);
  const lockup = lockupSvg("stacked", "dark", strokesFor(S)).replace('role="img"', `width="${w}" height="${h}" role="img"`);
  const titleHtml = title
    ? `<div style="margin-top:44px;font:600 46px/1.15 Inter, system-ui, sans-serif;color:${BRAND_COLORS.inkInverse};letter-spacing:-0.01em;max-width:1000px;text-align:center">${title.replace(/</g, "&lt;")}</div>`
    : "";
  await page.setViewportSize({ width: 1200, height: 630 });
  await page.setContent(
    `<!doctype html><html><head>${title ? '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@600&display=block">' : ""}</head>` +
      `<body style="margin:0;width:1200px;height:630px;background:${BRAND_COLORS.surfaceDark};display:flex;flex-direction:column;align-items:center;justify-content:center">` +
      `${lockup}${titleHtml}</body></html>`,
    { waitUntil: "networkidle" },
  );
  if (title) await page.evaluate(() => document.fonts.ready);
  return page.screenshot({ type: "png", clip: { x: 0, y: 0, width: 1200, height: 630 } });
}

/** Multi-resolution .ico with PNG-compressed entries (supported by every current browser and Windows). */
function ico(sizes, pngs) {
  const header = Buffer.alloc(6 + 16 * sizes.length);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(sizes.length, 4);
  let offset = header.length;
  sizes.forEach((s, i) => {
    const e = 6 + 16 * i;
    header.writeUInt8(s >= 256 ? 0 : s, e);
    header.writeUInt8(s >= 256 ? 0 : s, e + 1);
    header.writeUInt8(0, e + 2);
    header.writeUInt8(0, e + 3);
    header.writeUInt16LE(1, e + 4);
    header.writeUInt16LE(32, e + 6);
    header.writeUInt32LE(pngs[i].length, e + 8);
    header.writeUInt32LE(offset, e + 12);
    offset += pngs[i].length;
  });
  return Buffer.concat([header, ...pngs]);
}
