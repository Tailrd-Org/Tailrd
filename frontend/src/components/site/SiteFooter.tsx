import { Link } from "react-router-dom";
import { Logo } from "../brand";

/** Shared marketing footer. Section links use hash routes to the home page so
 *  they work from any marketing page (Landing hosts the scroll-to-hash effect). */
export default function SiteFooter() {
  return (
    <footer className="landing-footer">
      <div className="footer-inner">
        <div className="footer-col">
          <div className="footer-brand">
            <Logo theme="light" size={32} className="landing-logo-full" />
          </div>
          <p className="footer-tagline">Job applications, with AI used responsibly.<br />You stay the author.</p>
        </div>
        <div className="footer-col">
          <h4>Product</h4>
          <Link to={{ pathname: "/", hash: "#features" }}>Features</Link>
          <Link to="/pricing">Pricing</Link>
          <Link to={{ pathname: "/", hash: "#faq" }}>FAQ</Link>
        </div>
        <div className="footer-col">
          <h4>Company</h4>
          <Link to="/about">About</Link>
        </div>
        <div className="footer-col">
          <h4>Legal</h4>
          <Link to="/privacy">Privacy Policy</Link>
          <Link to="/terms">Terms of Service</Link>
          <Link to="/cookies">Cookie Policy</Link>
        </div>
      </div>
      <div className="footer-bottom">
        <p>© 2026 Tailrd. All rights reserved.</p>
      </div>
    </footer>
  );
}
