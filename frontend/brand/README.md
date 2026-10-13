# Tailrd brand

The logo system: the mark, the custom "Tailrd" lettering, four lockups, the app icon and the
favicon. Everything here is generated from one file,
[`src/components/brand/logo.constants.ts`](../src/components/brand/logo.constants.ts), so the React
component, the standalone SVGs and the PNGs can never drift apart.

```bash
cd frontend
node --experimental-strip-types --disable-warning=ExperimentalWarning scripts/brand/build-brand.mjs
#   (same as `npm run brand`; add --svg-only to skip PNGs, --og-title "Pricing" for a titled OG card)
```

Open [`preview.html`](preview.html) to see every variant on light and dark grounds plus the size ramp.

## Where the geometry comes from

There is no Figma source for the logo; the existing artwork is the source of truth:
`docs/Logo.jpeg` (the original, 1024 px) and `frontend/public/icon-512.png` (the cleanest render of
the mark; `logo-full.png` and `logo-icon.png` are crops of the same art).

Every shape was **fitted to that artwork by least squares**, not traced by eye:

| Part | Method | Overlap with the source |
| --- | --- | --- |
| Mark | Round-cap stroke model fitted to `icon-512.png` | IoU 0.957 (shipped SVG, rendered in Chromium) |
| Lettering | Parametric outlines (slabs, quarter-round corners, fillets) fitted to `docs/Logo.jpeg` | IoU 0.993 |
| Horizontal lockup | Both of the above, placed as measured | mark 0.92, wordmark 0.95 at the JPEG's native size |

The build spec this system started from (a reconstruction from a 147 x 57 px PNG) disagreed with
the artwork in several places. Where they differ, **the artwork won**, with these deliberate
exceptions:

| Value | Measured from the artwork | Spec | Shipped | Why |
| --- | --- | --- | --- | --- |
| Mark colour | `#5F45E3` | `#6247E5` | `#6247E5` (spec) | dE2000 0.74, below what the eye can see |
| Wordmark colour | `#040406` | `#09090B` | `#09090B` (spec) | dE2000 0.80 |
| Dark-mode, inverse and surface colours | (no dark artwork exists) | `#8B7BF0` `#FAFAFA` `#FFFFFF` `#0B0B10` | spec | |
| Ring gap | 31.9 deg, centred at 223 deg | 22 deg at 225 deg | measured | |
| Ring stroke | 1.73 | 1.5 | measured | the ring is 57% heavier than the plane |
| Plane and trail stroke | 1.10 | 1.5 | measured | |
| Plane | 7 points, 9 edges, with a keel fold | 2 triangles | measured | |
| Trail | 4 dashes on 2 parallel lines at 37 deg | 3 dashes | measured | |
| Wordmark face | custom lettering (no typeface matches; best of 137 fonts scored IoU 0.82) | Plus Jakarta Sans 600 | rebuilt lettering | |
| Mark to wordmark gap | 0.083 S | 0.143 S | measured | |
| Cap height | 0.450 S | 0.46 S | measured | |
| Cap-height centre | 0.003 S below the icon centre | level | level (spec) | the 1 px difference is noise |
| Lockup width | 2.69 S | 2.83 S | measured | |
| Stacked gap, app icon, favicon, stroke ramp | (no artwork exists) | section 4 and 5 | spec, adjusted | see "Constructions" |

Small clean-ups applied to the fit, each within about 1 px of the free fit: the keel meets the tail
panel exactly on its edge, the four dashes sit on two exactly parallel lines, and the lowercase stems
share one width (the capital T's stem is genuinely heavier and keeps its own).

## Tokens

| Token | Hex | Use |
| --- | --- | --- |
| `--brand-primary` | `#6247E5` | The mark on light backgrounds |
| `--brand-primary-dark-mode` | `#8B7BF0` | The mark on dark backgrounds (5.79:1 on `#0B0B10`) |
| `--brand-ink` | `#09090B` | The wordmark on light backgrounds |
| `--brand-ink-inverse` | `#FAFAFA` | The wordmark on dark backgrounds |
| `--brand-surface` | `#FFFFFF` | Default light background |
| `--brand-surface-dark` | `#0B0B10` | Default dark background |

The CSS custom properties live in `src/components/brand/brand.css`; the same values are
`BRAND_COLORS` in `logo.constants.ts`. The product UI's accent (`#533afd`, from the Stripe-style
palette) is a separate colour and is not a logo colour.

## Metrics

`S` is the icon box (48 units). The ring has r = 21.5 about the box centre.

| | Value |
| --- | --- |
| Icon box | S x S |
| Ring inset inside the box | 1.64 units (mirrored as the internal margin on every lockup edge) |
| Gap, icon box to wordmark | 3.97 units, 0.083 S |
| Cap height | 21.62 units, 0.450 S |
| x-height | 15.77 units (0.73 of cap height) |
| Vertical alignment | cap-height centre exactly level with the icon centre |
| Horizontal lockup | 128.95 x 48 units, 2.69 S |
| Stacked lockup | 78.62 x 79.9 units; cap top 0.18 S below the icon box |
| Wordmark only | 75.34 x 21.62 units |

### Stroke weight by rendered size

The component picks weights from the icon size. The artwork's own weights are the large-size tier.
Below it, each tier keeps the plane's lines at roughly one device pixel or more: thinner strokes
antialias into a washed-out, grainy line on 1x screens (a test enforces this).

| Rendered size | Ring | Plane and trail |
| --- | --- | --- |
| 64 px and up | 1.73 | 1.10 |
| 48 to 63 px | 2.10 | 1.35 |
| 32 to 47 px | 2.60 | 1.75 |
| 24 to 31 px | 2.80 | 1.90 |
| 20 to 23 px | 3.20 | 2.20 |
| under 20 px | 3.60 | 2.60 |

The standalone SVGs carry the artwork weights (1.73 / 1.10).

## Constructions

- **App icon.** White plane and trail on solid `#6247E5`, no ring (the tile contains the mark).
  The ring's footprint sets the scale (64% of the canvas), then the plane is optically centred.
  Stroke 1.91: the spec's heavier "2.6 of 1.5" applied to the measured weight; a flat 2.6 clogs the
  keel. Rounded tile radius 22.37%; a full-bleed version ships for platforms that mask it themselves.
- **Favicon and extension toolbar icons.** The real mark (ring, plane and trail) on a transparent
  ground, cropped to the ring, with strokes hinted per size (`ICON_STROKES`: 16 px 3.6/2.6, 32 px
  2.8/1.9, 48 px 2.1/1.35, 128 px the artwork weights). `favicon.svg` uses the 16 px weights and
  lightens the mark to `#8B7BF0` in dark mode so it reads on dark tab strips.

## Variants

| Theme | Mark | Wordmark | Use |
| --- | --- | --- | --- |
| `light` | `#6247E5` | `#09090B` | Default. App shell, docs, light marketing |
| `dark` | `#8B7BF0` | `#FAFAFA` | Dark UI |
| `mono-black` | `#09090B` | `#09090B` | Print, single-colour, partner one-pagers |
| `mono-white` | `#FFFFFF` | `#FFFFFF` | Over photos, over the brand purple, dark merch |
| `currentColor` | inherits | inherits | Buttons, disabled states, hover colour shifts |
| `appIcon` | `#FFFFFF` on `#6247E5` | | iOS, Android, PWA, desktop |
| favicon | `#6247E5` (`#8B7BF0` in dark mode) | | browser tab, extension toolbar |

## Using it in the app

```tsx
import { Logo, LogoSpinner } from "../components/brand";

<Logo theme="light" />                          // horizontal lockup, 32 px icon
<Logo variant="mark" theme="light" size={28} />
<Logo variant="stacked" theme="dark" size={64} animate />
<Logo variant="wordmark" theme="currentColor" />
<LogoSpinner size={32} />                       // trail dashes pulse left to right
```

`size` is the icon height in px; everything else derives from it. The root element is the `<svg>`
itself (it forwards its ref and any extra props), with `role="img"` and a `<title>` unless
`decorative`.

**This app has no dark mode, so pass `theme="light"`.** The default, `auto`, follows
`prefers-color-scheme` in CSS alone (no flash, no hydration mismatch), which on a light-only page
would draw a white wordmark on a white header for anyone whose OS is in dark mode.

## Rules

**Clear space.** Keep 0.25 S clear on all four sides of any lockup.

**Minimum sizes.** Mark 16 px (strokes are hinted heavier at small sizes).
Horizontal lockup 90 px wide. Stacked lockup 64 px wide.

**Never:**

- Fill the plane in the standard mark (outline only; only the favicon construction is filled)
- Close the gap in the ring
- Change the trail: it is exactly four dashes on two parallel lines, and the standard mark keeps it
- Even out the stroke weights: the ring is heavier than the plane by design
- Recolour the mark outside the tokens above
- Apply gradients, shadows, glows, outlines or bevels
- Rotate, skew, mirror or stretch it
- Set the wordmark in a typeface: it is custom lettering, so always use the paths
- Put the light-theme lockup on a busy photo (use `mono-white`)
- Place the purple mark on a purple background

## Files

```
frontend/
  src/components/brand/       Logo.tsx, LogoMark.tsx, Wordmark.tsx, LogoSpinner.tsx,
                              logo.constants.ts (the source of truth), brand.css, index.ts
  src/assets/brand/           13 standalone SVGs: role="img" + <title>, attributes only,
                              no ids or classes, viewBox only, wordmark as outlines
  public/                     favicon.ico (16/32/48), favicon.svg, favicon-96x96.png,
                              apple-touch-icon.png, icon-192.png, icon-512.png,
                              icon-512-maskable.png, og-image.png, site.webmanifest
  brand/                      this file, preview.html, png/ (@1x @2x @3x of every lockup x theme,
                              app icon sizes), email-signature@2x.png, social-avatar-400.png,
                              tailrd-app-icon-full-bleed.svg
  scripts/brand/build-brand.mjs  also writes chrome-extension/assets/icon-*.png and
                                 chrome-extension/src/content/brandLogo.ts (inline SVG for the panel)
```

`public/logo-full.png` and `public/logo-icon.png` stay: transactional emails
(`backend/services/email_service.py`) still link to `logo-full.png`.

The path data is pinned by `src/__tests__/brand-logo.test.tsx`, and
`src/__tests__/brand-assets.test.ts` checks the generated files. If the artwork ever changes on
purpose, update `logo.constants.ts`, run the build, and update the pins in the same commit.
