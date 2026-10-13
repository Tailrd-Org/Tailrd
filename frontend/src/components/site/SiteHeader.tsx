import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import type { To } from "react-router-dom";
import { useAuth } from "../../auth/useAuth";
import { Logo } from "../brand";

/** Section links use hash routes so they work from any page; the Landing page
 *  scrolls to the hash on mount / hash change. */
const NAV_LINKS: { to: To; label: string }[] = [
  { to: { pathname: "/", hash: "#features" }, label: "Features" },
  { to: "/pricing", label: "Pricing" },
  { to: { pathname: "/", hash: "#success-story" }, label: "Results" },
  { to: { pathname: "/", hash: "#faq" }, label: "FAQ" },
];

/** Shared marketing top nav. Under 768px the inline links collapse into a
 *  hamburger menu. They used to be `display:none` with no replacement, which
 *  made Features / Pricing / Results / FAQ unreachable on a phone. */
export default function SiteHeader() {
  const navigate = useNavigate();
  const location = useLocation();
  const { isAuthenticated } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  // Close on navigation (the hash links keep you on the same route, so watch both).
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname, location.hash]);

  // Close on Escape.
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  const go = (path: string) => {
    setMenuOpen(false);
    navigate(path);
  };

  return (
    <nav className="landing-nav">
      <div className="landing-nav-inner">
        <div className="landing-brand">
          <Link to="/" aria-label="Tailrd home">
            <Logo theme="light" size={32} className="landing-logo-full" />
          </Link>
        </div>
        <div className="landing-nav-links">
          {NAV_LINKS.map((l) => (
            <Link key={l.label} to={l.to} className="nav-link-item">
              {l.label}
            </Link>
          ))}
        </div>
        <div className="landing-nav-actions">
          {isAuthenticated ? (
            <button className="btn-cta nav-cta" onClick={() => go("/app")}>Dashboard</button>
          ) : (
            <>
              <button className="btn-ghost nav-login" onClick={() => go("/sign-in")}>Log in</button>
              <button className="btn-cta nav-cta" onClick={() => go("/sign-up")}>Sign up</button>
            </>
          )}
          <button
            type="button"
            className="landing-nav-toggle"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            aria-expanded={menuOpen}
            aria-controls="site-menu"
            onClick={() => setMenuOpen((v) => !v)}
          >
            <span className={`nav-burger${menuOpen ? " open" : ""}`} aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
          </button>
        </div>
      </div>

      {menuOpen && (
        <>
          <div className="landing-menu-backdrop" onClick={() => setMenuOpen(false)} aria-hidden="true" />
          <div className="landing-menu-panel" id="site-menu">
            {NAV_LINKS.map((l) => (
              <Link
                key={l.label}
                to={l.to}
                className="landing-menu-link"
                onClick={() => setMenuOpen(false)}
              >
                {l.label}
              </Link>
            ))}
            {!isAuthenticated && (
              <button type="button" className="landing-menu-link landing-menu-login" onClick={() => go("/sign-in")}>
                Log in
              </button>
            )}
          </div>
        </>
      )}
    </nav>
  );
}
