import { createRef } from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Logo, LogoSpinner, MARK, WORDMARK, strokesFor } from "../components/brand";

// The mark and lettering are a measured fit to the source artwork. These pins
// make any change deliberate: update them only together with the brand assets
// (npm run brand) and a note in frontend/brand/README.md.
describe("brand geometry is pinned", () => {
  it("keeps the mark's path data", () => {
    expect(MARK.ring).toBe("M4.92 33.91A21.5 21.5 0 1 1 13.05 42.5");
    expect(MARK.plane).toBe("M9.61 22.65L37.97 12.83L29.99 35.94L23.48 31.59L18.23 36.08L16.41 27.5Z");
    expect(MARK.folds).toEqual([
      "M16.41 27.5L37.97 12.83",
      "M37.97 12.83L20.41 29.53L18.23 36.08",
      "M20.41 29.53L23.48 31.59",
    ]);
    expect(MARK.trail).toEqual([
      "M8.34 36.17L5.69 38.17",
      "M10.01 39.72L5.96 42.78",
      "M13.44 32.33L10.53 34.52",
      "M15.06 35.91L12.01 38.22",
    ]);
    expect([MARK.ringStroke, MARK.detailStroke]).toEqual([1.73, 1.1]);
  });

  it("keeps the lettering's path data", () => {
    expect(WORDMARK.glyphs).toEqual({
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
    });
  });

  it("keeps exactly four trail dashes and a broken ring", () => {
    expect(MARK.trail).toHaveLength(4);
    // A closed ring would end where it starts; the gap means it does not.
    expect(MARK.ring.startsWith("M4.92 33.91")).toBe(true);
    expect(MARK.ring.endsWith("13.05 42.5")).toBe(true);
  });
});

describe("<Logo />", () => {
  it("renders the svg itself as the root, named Tailrd, 32px tall by default", () => {
    const { container } = render(<Logo />);
    const svg = container.firstElementChild as SVGSVGElement;
    expect(svg.tagName.toLowerCase()).toBe("svg");
    expect(screen.getByRole("img", { name: "Tailrd" })).toBe(svg);
    expect(svg.querySelector("title")?.textContent).toBe("Tailrd");
    expect(svg).toHaveAttribute("height", "32");
    expect(svg).toHaveAttribute("width", String(Math.round((128.95 / 48) * 32 * 100) / 100));
    expect(svg).toHaveAttribute("data-theme", "auto");
    expect(container.querySelector("div")).toBeNull();
  });

  it("derives every variant's box from the icon size", () => {
    const dims = (variant: "horizontal" | "stacked" | "mark" | "wordmark" | "appIcon") => {
      const { container } = render(<Logo variant={variant} size={48} />);
      const svg = container.firstElementChild!;
      return [svg.getAttribute("viewBox"), svg.getAttribute("width"), svg.getAttribute("height")];
    };
    expect(dims("horizontal")).toEqual(["0 0 128.95 48", "128.95", "48"]);
    expect(dims("stacked")).toEqual(["0 0 78.62 79.9", "78.62", "79.9"]);
    expect(dims("mark")).toEqual(["0 0 48 48", "48", "48"]);
    expect(dims("wordmark")).toEqual(["0 0 75.34 21.62", "75.34", "21.62"]);
    expect(dims("appIcon")).toEqual(["0 0 48 48", "48", "48"]);
  });

  it("picks stroke weights from the size ramp", () => {
    const weights = (size: number) => {
      const { container } = render(<Logo variant="mark" size={size} />);
      const ring = container.querySelector(`path[d="${MARK.ring}"]`)!;
      return [ring.getAttribute("stroke-width"), ring.nextElementSibling!.getAttribute("stroke-width")];
    };
    expect(weights(128)).toEqual(["1.73", "1.1"]);
    expect(weights(64)).toEqual(["1.73", "1.1"]);
    expect(weights(48)).toEqual(["2.1", "1.35"]);
    expect(weights(32)).toEqual(["2.6", "1.75"]);
    expect(weights(24)).toEqual(["2.8", "1.9"]);
    expect(weights(20)).toEqual(["3.2", "2.2"]);
    expect(weights(16)).toEqual(["3.6", "2.6"]);
  });

  it("keeps the plane's lines at about one device pixel or more at every small size", () => {
    for (const size of [16, 20, 24, 28, 32, 44, 48, 63]) {
      expect(strokesFor(size).detail * (size / 48)).toBeGreaterThanOrEqual(0.86);
    }
  });

  it("draws the real mark, ring and all, even at 16px", () => {
    const { container } = render(<Logo variant="mark" size={16} />);
    expect(container.querySelector(`path[d="${MARK.ring}"]`)).not.toBeNull();
    expect(container.querySelectorAll("path")).toHaveLength(1 + 1 + MARK.folds.length + MARK.trail.length);
  });

  it("sets explicit theme colours inline and leaves auto to the stylesheet", () => {
    const vars = (theme: "auto" | "light" | "dark" | "mono-black" | "mono-white" | "currentColor") => {
      const { container } = render(<Logo theme={theme} />);
      const s = (container.firstElementChild as SVGSVGElement).style;
      return [s.getPropertyValue("--tailrd-logo-mark"), s.getPropertyValue("--tailrd-logo-ink")];
    };
    expect(vars("auto")).toEqual(["", ""]);
    expect(vars("light")).toEqual(["#6247E5", "#09090B"]);
    expect(vars("dark")).toEqual(["#8B7BF0", "#FAFAFA"]);
    expect(vars("mono-black")).toEqual(["#09090B", "#09090B"]);
    expect(vars("mono-white")).toEqual(["#FFFFFF", "#FFFFFF"]);
    expect(vars("currentColor")).toEqual(["currentColor", "currentColor"]);
  });

  it("paints the mark and wordmark through the theme variables, so currentColor inherits", () => {
    const { container } = render(<Logo theme="currentColor" />);
    const markGroup = container.querySelector(`path[d="${MARK.ring}"]`)!.parentElement!;
    expect(markGroup.getAttribute("style")).toContain("var(--tailrd-logo-mark");
    const word = container.querySelector("path[transform]")!;
    expect(word.getAttribute("style")).toContain("var(--tailrd-logo-ink");
  });

  it("hides a decorative logo and drops its title", () => {
    const { container } = render(<Logo decorative />);
    const svg = container.firstElementChild!;
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).not.toHaveAttribute("role");
    expect(svg.querySelector("title")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("forwards its ref and spreads extra props onto the svg", () => {
    const ref = createRef<SVGSVGElement>();
    render(<Logo ref={ref} title="Tailrd home" data-testid="brand" className="extra" style={{ display: "block" }} />);
    const svg = screen.getByTestId("brand");
    expect(ref.current).toBe(svg);
    expect(svg).toHaveClass("tailrd-logo", "extra");
    expect(svg.style.display).toBe("block");
    expect(screen.getByRole("img", { name: "Tailrd home" })).toBe(svg);
  });

  it("marks every stroke for the draw-on intro when animated", () => {
    const { container } = render(<Logo variant="mark" size={64} animate />);
    const strokes = container.querySelectorAll("path[pathLength='1']");
    expect(strokes).toHaveLength(1 + 1 + MARK.folds.length + MARK.trail.length);
    expect(container.firstElementChild).toHaveClass("tailrd-logo--animate");
  });

  it("uses fixed brand colours for the app icon", () => {
    const { container } = render(<Logo variant="appIcon" theme="dark" />);
    expect(container.querySelector("rect")).toHaveAttribute("fill", "#6247E5");
    expect(container.querySelector("g[stroke='#FFFFFF']")).not.toBeNull();
  });
});

describe("<LogoSpinner />", () => {
  it("pulses the four trail dashes left to right over 1.2s", () => {
    render(<LogoSpinner />);
    const svg = screen.getByRole("img", { name: "Loading" });
    const dashes = svg.querySelectorAll(".tailrd-logo-spinner__dash");
    expect(dashes).toHaveLength(4);
    expect([...dashes].map((d) => (d as SVGElement).style.animationDelay)).toEqual(["0s", "0.3s", "0.6s", "0.9s"]);
  });
});
