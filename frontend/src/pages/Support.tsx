import { Link } from "react-router-dom";
import { Logo } from "../components/brand";
import "../privacy.css";

/**
 * Public Support page (route: /support), the Chrome Web Store listing's
 * support URL points here. Same flat document layout as the Privacy page.
 */
export default function Support() {
  return (
    <div className="legal-page">
      <div className="legal-doc">
        {/* Brand */}
        <Link to="/" className="legal-brand" aria-label="Back to home">
          <Logo variant="mark" theme="light" size={30} decorative className="legal-brand-img" />
          <span>Tailrd</span>
        </Link>

        <h1 className="legal-title">Support</h1>
        <p className="legal-lead">
          Questions, bug reports, or feedback about Tailrd or the Tailrd Chrome
          extension. We read everything and reply as fast as we can.
        </p>

        <h2>Contact us</h2>
        <p>
          Email <a href="mailto:support@tailrd.ca">support@tailrd.ca</a> and include, if you
          can, the page you were on (a link to the job posting helps enormously) and what you
          expected to happen. Screenshots are gold.
        </p>

        <h2>Quick fixes for common issues</h2>
        <ul>
          <li>
            <strong>The panel doesn&rsquo;t appear on a job application.</strong> Click the
            Tailrd icon in the Chrome toolbar to open it, and make sure you&rsquo;re on the
            application form itself (not the job description page).
          </li>
          <li>
            <strong>&ldquo;Connect your Tailrd account&rdquo; keeps showing.</strong> Sign in
            at <a href="https://www.tailrd.ca">www.tailrd.ca</a> first, then press Connect in
            the panel again.
          </li>
          <li>
            <strong>Some fields were left blank.</strong> That&rsquo;s deliberate: Tailrd only
            fills answers it can ground in your profile and never guesses. Add the missing
            details under <em>Your profile on this device</em> in the panel and press{" "}
            <em>Fill from my profile</em> again.
          </li>
          <li>
            <strong>An outdated résumé got attached.</strong> Edits made in the web app are
            picked up automatically; if something looks stale, reopen the panel so it re-syncs,
            then re-attach.
          </li>
          <li>
            <strong>Anything else acting oddly.</strong> Reload the page, or toggle the
            extension off and on at <code>chrome://extensions</code>, then tell us about it
            so we can fix the root cause.
          </li>
        </ul>

        <h2>Your data</h2>
        <p>
          What we store and why is covered in our <Link to="/privacy">Privacy Policy</Link>.
          To remove your data, email us from your account address and we&rsquo;ll take care
          of it.
        </p>
      </div>
    </div>
  );
}
