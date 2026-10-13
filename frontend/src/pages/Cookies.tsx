import { Link } from "react-router-dom";
import { Logo } from "../components/brand";
import "../privacy.css";

/** Public Cookie Policy (route: /cookies). Reuses the legal reading layout. */
export default function Cookies() {
  return (
    <div className="legal-page">
      <div className="legal-doc">
        <Link to="/" className="legal-brand" aria-label="Back to home">
          <Logo variant="mark" theme="light" size={30} decorative className="legal-brand-img" />
          <span>Tailrd</span>
        </Link>
        <h1 className="legal-title">Cookie Policy</h1>
        <p className="legal-updated">Last updated July 10, 2026</p>

        <p>
          This Cookie Policy explains how Tailrd (“we,” “us,” or “our”) uses
          cookies and similar technologies when you visit{" "}
          <a href="https://www.tailrd.ca">www.tailrd.ca</a> or use our browser
          extension. It should be read together with our{" "}
          <a href="/privacy">Privacy Policy</a>.
        </p>

        <h2>What are cookies?</h2>
        <p>
          Cookies are small text files stored on your device by your browser.
          Similar technologies such as browser local storage can also store small
          amounts of data on your device.
        </p>

        <h2>How we use them</h2>
        <p>
          We use only what is necessary to sign you in and keep you signed in. We
          do <strong>not</strong> use advertising or third-party tracking cookies.
        </p>
        <ul>
          <li>
            <strong>Essential authentication cookie</strong>: a secure, HttpOnly
            cookie named <code>refresh_token</code> that keeps your session active
            so you don't have to sign in on every visit. It is scoped to our
            authentication endpoints and cannot be read by JavaScript.
          </li>
          <li>
            <strong>Local storage</strong>: we store a short-lived access token
            in your browser's local storage to authorize requests while you use
            the app. It is cleared when you sign out.
          </li>
        </ul>

        <h2>Managing cookies</h2>
        <p>
          Because these technologies are strictly necessary to provide the
          Services, disabling them will prevent you from signing in. You can clear
          cookies and local storage at any time through your browser settings;
          doing so will sign you out.
        </p>

        <h2>Changes to this policy</h2>
        <p>
          If we introduce analytics or other non-essential cookies in the future,
          we will update this policy and, where required by law, ask for your
          consent first.
        </p>

        <h2>Contact</h2>
        <p>
          Questions about this Cookie Policy? Contact us at{" "}
          <a href="mailto:support@tailrd.ca">support@tailrd.ca</a>.
        </p>
      </div>
    </div>
  );
}
