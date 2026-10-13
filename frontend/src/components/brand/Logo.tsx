import { forwardRef, useId, type CSSProperties, type SVGProps } from "react";
import "./brand.css";
import { LOCKUP, strokesFor, themeVars, type LogoTheme, type LogoVariant } from "./logo.constants";
import { AppIconGraphic, MarkGraphic } from "./LogoMark";
import { WordmarkGraphic } from "./Wordmark";

export interface LogoProps extends Omit<SVGProps<SVGSVGElement>, "ref" | "children" | "width" | "height" | "viewBox"> {
  variant?: LogoVariant;
  theme?: LogoTheme;
  /** The icon height in px. Every other dimension derives from it. */
  size?: number;
  /** Accessible name. */
  title?: string;
  /** Hides the logo from assistive tech (aria-hidden) and drops the title. */
  decorative?: boolean;
  /** One-shot draw-on intro: the ring, then the plane, 600ms. Skipped under reduced motion. */
  animate?: boolean;
}

/** viewBox size per variant, in mark units (the icon box is 48 x 48). */
const FRAME: Record<LogoVariant, { w: number; h: number }> = {
  horizontal: { w: LOCKUP.horizontal.width, h: LOCKUP.horizontal.height },
  stacked: { w: LOCKUP.stacked.width, h: LOCKUP.stacked.height },
  mark: { w: 48, h: 48 },
  wordmark: { w: LOCKUP.wordmark.width, h: LOCKUP.wordmark.height },
  appIcon: { w: 48, h: 48 },
};

const round2 = (n: number) => Math.round(n * 100) / 100;

export const Logo = forwardRef<SVGSVGElement, LogoProps>(function Logo(
  {
    variant = "horizontal",
    theme = "auto",
    size = 32,
    title = "Tailrd",
    decorative = false,
    animate = false,
    className,
    style,
    ...rest
  },
  ref,
) {
  const titleId = useId();
  const frame = FRAME[variant];
  const px = size / 48;
  const { ring, detail } = strokesFor(size);
  const mark = <MarkGraphic ring={ring} detail={detail} animate={animate} />;

  return (
    <svg
      ref={ref}
      xmlns="http://www.w3.org/2000/svg"
      viewBox={`0 0 ${frame.w} ${frame.h}`}
      width={round2(frame.w * px)}
      height={round2(frame.h * px)}
      className={["tailrd-logo", animate && "tailrd-logo--animate", className].filter(Boolean).join(" ")}
      data-theme={theme}
      style={{ ...(themeVars(theme) as CSSProperties | undefined), ...style }}
      role={decorative ? undefined : "img"}
      aria-labelledby={decorative ? undefined : titleId}
      aria-hidden={decorative ? true : undefined}
      {...rest}
    >
      {!decorative && <title id={titleId}>{title}</title>}
      {variant === "mark" && mark}
      {variant === "horizontal" && (
        <>
          {mark}
          <WordmarkGraphic x={LOCKUP.horizontal.wordmarkX} y={LOCKUP.horizontal.wordmarkY} animate={animate} />
        </>
      )}
      {variant === "stacked" && (
        <>
          <g transform={`translate(${LOCKUP.stacked.markX} 0)`}>{mark}</g>
          <WordmarkGraphic x={LOCKUP.stacked.wordmarkX} y={LOCKUP.stacked.wordmarkY} animate={animate} />
        </>
      )}
      {variant === "wordmark" && <WordmarkGraphic animate={animate} />}
      {variant === "appIcon" && <AppIconGraphic />}
    </svg>
  );
});
