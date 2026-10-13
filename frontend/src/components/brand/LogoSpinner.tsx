import { forwardRef, useId, type CSSProperties, type SVGProps } from "react";
import "./brand.css";
import { MARK, strokesFor, themeVars, type LogoTheme } from "./logo.constants";
import { MarkGraphic } from "./LogoMark";

export interface LogoSpinnerProps extends Omit<SVGProps<SVGSVGElement>, "ref" | "children" | "width" | "height" | "viewBox"> {
  /** Rendered size in px. */
  size?: number;
  theme?: LogoTheme;
  /** Accessible name. */
  label?: string;
}

// One full left-to-right pass per loop: 1.2s split evenly across the dashes.
const STEP = 1.2 / MARK.trail.length;

/**
 * Loading mark: the ring and plane hold still while the trail dashes pulse in
 * sequence, left to right, on a 1.2s loop. Static under reduced motion.
 */
export const LogoSpinner = forwardRef<SVGSVGElement, LogoSpinnerProps>(function LogoSpinner(
  { size = 32, theme = "auto", label = "Loading", className, style, ...rest },
  ref,
) {
  const titleId = useId();
  const { ring, detail } = strokesFor(size);
  return (
    <svg
      ref={ref}
      xmlns="http://www.w3.org/2000/svg"
      viewBox={MARK.viewBox}
      width={size}
      height={size}
      className={["tailrd-logo", "tailrd-logo-spinner", className].filter(Boolean).join(" ")}
      data-theme={theme}
      style={{ ...(themeVars(theme) as CSSProperties | undefined), ...style }}
      role="img"
      aria-labelledby={titleId}
      {...rest}
    >
      <title id={titleId}>{label}</title>
      <MarkGraphic
        ring={ring}
        detail={detail}
        dashClassName="tailrd-logo-spinner__dash"
        dashStyle={(i) => ({ animationDelay: `${Number((i * STEP).toFixed(2))}s` })}
      />
    </svg>
  );
});
