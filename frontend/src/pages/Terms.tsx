import { Link } from "react-router-dom";
import { Logo } from "../components/brand";
import "../privacy.css";

/** Public Terms of Service (route: /terms). Reuses the legal reading layout. */
export default function Terms() {
  return (
    <div className="legal-page">
      <div className="legal-doc">
        <Link to="/" className="legal-brand" aria-label="Back to home">
          <Logo variant="mark" theme="light" size={30} decorative className="legal-brand-img" />
          <span>Tailrd</span>
        </Link>
        <h1 className="legal-title">Terms of Service</h1>
        <p className="legal-updated">Last updated July 10, 2026</p>

        <p>
          These Terms of Service (“Terms”) govern your access to and use of
          Tailrd (“we,” “us,” or “our”), including our website at{" "}
          <a href="https://www.tailrd.ca">www.tailrd.ca</a> and our browser
          extension (collectively, the “Services”). By creating an account or
          using the Services, you agree to these Terms. If you do not agree, do
          not use the Services.
        </p>

        <h2>1. Eligibility &amp; accounts</h2>
        <p>
          You must be at least 16 years old to use the Services. You are
          responsible for the accuracy of the information you provide and for
          safeguarding your account credentials. You are responsible for all
          activity that occurs under your account.
        </p>

        <h2>2. Acceptable use</h2>
        <p>
          You agree to use the Services only for lawful purposes and in
          accordance with these Terms. You will not misuse the Services,
          interfere with their operation, attempt to access them using a method
          other than the interfaces we provide, or use them to submit false,
          misleading, or unauthorized job applications.
        </p>

        <h2>3. Your content</h2>
        <p>
          You retain ownership of the résumés, profile data, and other content
          you provide (“Your Content”). You grant us a limited license to
          process Your Content solely to operate and provide the Services to you,
          as described in our <a href="/privacy">Privacy Policy</a>.
        </p>

        <h2>4. AI-generated output</h2>
        <p>
          The Services use AI to suggest résumé edits and offer talking points for
          cover letters and application questions. AI output may contain errors. You are
          responsible for reviewing all output before submitting any application,
          and you remain solely responsible for the applications you submit.
        </p>

        <h2>5. Subscriptions &amp; pricing</h2>
        <p>
          Paid plans, where offered, are billed on the cadence shown at checkout
          and prices are stated in Canadian dollars (CAD). Pricing displayed on
          the site is subject to change. Where required by law, you may have
          rights to cancel or obtain a refund.
        </p>

        <h2>6. Third-party services</h2>
        <p>
          The Services interact with third-party job boards and applicant
          tracking systems that we do not control. We are not responsible for the
          availability, accuracy, or policies of those third parties, and your
          use of them may be subject to their own terms.
        </p>

        <h2>7. Disclaimers</h2>
        <p>
          The Services are provided “as is” and “as available” without warranties
          of any kind, whether express or implied, including fitness for a
          particular purpose. We do not warrant that the Services will be
          uninterrupted, error-free, or that they will result in any job offer or
          interview.
        </p>

        <h2>8. Limitation of liability</h2>
        <p>
          To the maximum extent permitted by law, Tailrd will not be liable for
          any indirect, incidental, special, consequential, or punitive damages,
          or any loss of data, opportunities, or profits, arising out of or
          related to your use of the Services.
        </p>

        <h2>9. Termination</h2>
        <p>
          You may stop using the Services at any time. We may suspend or terminate
          your access if you violate these Terms or use the Services in a way that
          could cause harm to us or others.
        </p>

        <h2>10. Governing law</h2>
        <p>
          {/* OWNER TO CONFIRM province, Privacy references Quebec (Law 25). */}
          These Terms are governed by the laws of the Province of Quebec and the
          federal laws of Canada applicable therein, without regard to conflict
          of laws principles.
        </p>

        <h2>11. Changes to these Terms</h2>
        <p>
          We may update these Terms from time to time. When we do, we will revise
          the “Last updated” date above. Your continued use of the Services after
          changes take effect constitutes acceptance of the revised Terms.
        </p>

        <h2>12. Contact</h2>
        <p>
          Questions about these Terms? Contact us at{" "}
          <a href="mailto:support@tailrd.ca">support@tailrd.ca</a>.
        </p>
      </div>
    </div>
  );
}
