import { Link } from "react-router-dom";
import { Logo } from "../components/brand";
import "../privacy.css";

/**
 * Public Privacy Policy page (route: /privacy).
 *
 * Flat legal-document layout (Termly / Jobright style): light-grey page,
 * centered reading column, uppercase headings, "Summary of Key Points",
 * table of contents, and numbered sections.
 *
 * Written for a worldwide audience, Quebec (Law 25), Canada (PIPEDA),
 * EU/EEA + UK (GDPR / UK GDPR), California (CCPA/CPRA), and other regions.
 * Placeholder-free: contact/jurisdiction details are filled in for publication.
 */
export default function Privacy() {
  return (
    <div className="legal-page">
      <div className="legal-doc">
        {/* Brand */}
        <Link to="/" className="legal-brand" aria-label="Back to home">
          <Logo variant="mark" theme="light" size={30} decorative className="legal-brand-img" />
          <span>Tailrd</span>
        </Link>

        {/* Title */}
        <h1 className="legal-title">Privacy Policy</h1>
        <p className="legal-updated">Last updated July 10, 2026</p>

        {/* Intro */}
        <p>
          This Privacy Policy for <strong>Tailrd</strong> (“<strong>we</strong>,”
          “<strong>us</strong>,” or “<strong>our</strong>”) describes how and why we may
          collect, store, use, and/or share (“<strong>process</strong>”) your personal
          information when you use our services (“<strong>Services</strong>”), including
          when you:
        </p>
        <ul>
          <li>Visit our website at <a href="https://www.tailrd.ca">www.tailrd.ca</a>, or any website of ours that links to this Privacy Policy.</li>
          <li>Use <strong>Tailrd</strong> and our browser extension to suggest résumé edits, offer cover letter ideas, match you with jobs, and help you fill applications.</li>
          <li>Use our AI features, automation, or external API integrations.</li>
          <li>Engage with us in other related ways, including any sales, marketing, or events.</li>
        </ul>
        <p>
          <strong>Questions or concerns?</strong> Reading this Privacy Policy will help you
          understand your privacy rights and choices. We are responsible for making
          decisions about how your personal information is processed. If you do not agree
          with our policies and practices, please do not use our Services. If you still
          have questions or concerns, contact us at <a href="mailto:support@tailrd.ca">support@tailrd.ca</a>.
        </p>

        {/* Summary of key points */}
        <h2 id="summary">Summary of Key Points</h2>
        <p className="legal-lead">
          This summary provides key points from our Privacy Policy, but you can find more
          detail on any topic in the full sections below, or by using the table of contents
          to jump to the section you are looking for.
        </p>
        <p>
          <strong>What personal information do we process?</strong> When you use our
          Services, we may process personal information depending on how you interact with
          us and the Services, the choices you make, and the features you use. Learn more in{" "}
          <a href="#collect">Personal information we collect</a>.
        </p>
        <p>
          <strong>Do we process sensitive personal information?</strong> We do not seek to
          collect sensitive information (such as racial or ethnic origin, or demographic
          self-identification) except where you voluntarily provide it or where required for
          equal-opportunity forms, and only where permitted by law or with your consent.
        </p>
        <p>
          <strong>Do we collect information from third parties?</strong> We may collect
          information from authentication providers, payment processors, analytics
          providers, and job-board systems you interact with. Learn more in{" "}
          <a href="#sources">How we collect it</a>.
        </p>
        <p>
          <strong>How do we process your information?</strong> To provide, improve, and
          administer our Services, communicate with you, for security and fraud prevention,
          and to comply with law. Learn more in <a href="#why">Why we use your information</a>.
        </p>
        <p>
          <strong>How do we keep your information safe?</strong> We use organizational and
          technical safeguards, but no electronic transmission or storage is 100% secure.
          Learn more in <a href="#security">Security</a>.
        </p>
        <p>
          <strong>What are your rights?</strong> Depending on where you live, you may have
          rights to access, correct, delete, or port your information, and to withdraw
          consent. Learn more in <a href="#rights">Your privacy rights</a>.
        </p>

        {/* Table of contents */}
        <h2 id="toc">Table of Contents</h2>
        <ol className="legal-toc">
          <li><a href="#intro">Introduction &amp; scope</a></li>
          <li><a href="#controller">Who we are &amp; how to reach us</a></li>
          <li><a href="#collect">Personal information we collect</a></li>
          <li><a href="#sources">How we collect it</a></li>
          <li><a href="#why">Why we use your information</a></li>
          <li><a href="#legal-basis">Legal bases for processing</a></li>
          <li><a href="#cookies">Cookies, analytics &amp; tracking</a></li>
          <li><a href="#account">Account &amp; authentication data</a></li>
          <li><a href="#device">Device, usage, log &amp; crash data</a></li>
          <li><a href="#location">Location data</a></li>
          <li><a href="#comms">Notifications &amp; communications</a></li>
          <li><a href="#payments">Payments, subscriptions &amp; billing</a></li>
          <li><a href="#ai">AI features &amp; third-party integrations</a></li>
          <li><a href="#sharing">How we share information</a></li>
          <li><a href="#transfers">International data transfers</a></li>
          <li><a href="#retention">Data retention &amp; destruction</a></li>
          <li><a href="#security">Security</a></li>
          <li><a href="#rights">Your privacy rights</a></li>
          <li><a href="#quebec">Quebec (Law 25) disclosures</a></li>
          <li><a href="#gdpr">GDPR / UK GDPR rights</a></li>
          <li><a href="#ccpa">California (CCPA/CPRA) rights</a></li>
          <li><a href="#children">Children's privacy</a></li>
          <li><a href="#breach">Breach notification</a></li>
          <li><a href="#changes">Changes to this policy</a></li>
          <li><a href="#contact">Contact us</a></li>
        </ol>

        <section id="intro">
          <h2>1. Introduction &amp; scope</h2>
          <p>
            This Privacy Policy explains how <strong>Tailrd</strong> collects, uses,
            discloses, and protects personal information when you use <strong>Tailrd</strong> and
            any related websites, browser extensions, and services (together, the “Service”).
          </p>
          <p>
            We designed this policy for a worldwide audience. Depending on where you live,
            additional rights and protections may apply, including under Quebec’s <em>Act
            respecting the protection of personal information in the private sector</em> (as
            amended by Law 25), Canada’s PIPEDA, the EU/EEA and UK General Data Protection
            Regulation (GDPR / UK GDPR), and the California Consumer Privacy Act as amended by
            the CPRA (CCPA/CPRA). Where a specific law grants you stronger rights than
            described here, that law governs. This policy is adaptable to the laws of
            Canada and other regions where applicable privacy laws apply.
          </p>
          <p>
            By using the Service, you acknowledge that you have read and understood this
            policy. If you do not agree with it, please do not use the Service.
          </p>
        </section>

        <section id="controller">
          <h2>2. Who we are &amp; how to reach us</h2>
          <p>
            The organization responsible for your personal information (the “data
            controller,” or under Quebec law the entity accountable for the information) is:
          </p>
          <ul>
            <li><strong>Company:</strong> Tailrd</li>
            <li><strong>Person in charge of personal information / Privacy Officer:</strong> Wissam Elmasry</li>
            <li><strong>Contact email:</strong> support@tailrd.ca</li>
            <li><strong>Primary jurisdiction:</strong> Canada</li>
            <li><strong>Hosting country:</strong> the United States</li>
          </ul>
          <p>
            You may contact our Privacy Officer at any time using the details above to ask
            questions, exercise your rights, or make a complaint.
          </p>
        </section>

        <section id="collect">
          <h2>3. Personal information we collect</h2>
          <p>We collect the following categories of personal information:</p>
          <ul>
            <li><strong>Identity &amp; contact data</strong>: name, email address, phone number, links you provide (e.g., LinkedIn, personal website).</li>
            <li><strong>Profile &amp; job-search data</strong>: résumé/CV content, work history, education, skills, job titles, preferred locations, work authorization, remote preferences, and answers you save to reuse on applications.</li>
            <li><strong>Account &amp; authentication data</strong>: credentials, session tokens, and connected-device records.</li>
            <li><strong>Application &amp; activity data</strong>: jobs you view, applications you submit or track, and how you interact with the Service.</li>
            <li><strong>Device, usage &amp; log data</strong>: see Section 9.</li>
            <li><strong>Payment &amp; subscription data</strong>: where applicable (see Section 12).</li>
            <li><strong>Communications</strong>: messages, feedback, and support requests you send us.</li>
            <li><strong>Location data</strong>: where applicable (see Section 10).</li>
          </ul>
          <p>
            We do not intentionally collect special-category or sensitive information (such
            as health, race, or religious data) except where you voluntarily include it in a
            résumé or application answer, or where you provide demographic
            self-identification for equal-opportunity forms. Where such information is subject
            to heightened protection, we handle it accordingly and, where required, with your
            explicit consent.
          </p>
        </section>

        <section id="sources">
          <h2>4. How we collect it</h2>
          <p>We obtain personal information in three main ways:</p>
          <ul>
            <li><strong>Directly from you</strong>: when you create an account, upload a résumé, complete your profile, save application answers, contact support, or otherwise use the Service.</li>
            <li><strong>Automatically through the app</strong>: through cookies, similar technologies, and server logs when you use our website, app, or browser extension (see Sections 7 and 9).</li>
            <li><strong>From third-party services</strong>: such as authentication providers, payment processors, analytics providers, and job-board or application-tracking systems you interact with through the Service ([Third-Party Services]).</li>
          </ul>
        </section>

        <section id="why">
          <h2>5. Why we use your information</h2>
          <p>We use personal information to:</p>
          <ul>
            <li>Provide, operate, and maintain the Service, including creating and managing your account.</li>
            <li>Suggest résumé edits, offer cover letter ideas, match you with jobs, and help you fill applications.</li>
            <li>Personalize your experience and remember your preferences and saved answers.</li>
            <li>Process payments and manage subscriptions, where applicable.</li>
            <li>Communicate with you, including service messages, product updates, and support responses.</li>
            <li>Send notifications, alerts, and emails you have enabled.</li>
            <li>Monitor, secure, debug, and improve the Service, including analytics and crash diagnostics.</li>
            <li>Detect, prevent, and respond to fraud, abuse, and security incidents.</li>
            <li>Comply with legal obligations and enforce our terms.</li>
          </ul>
        </section>

        <section id="legal-basis">
          <h2>6. Legal bases for processing</h2>
          <p>
            Where the GDPR, UK GDPR, Quebec law, or similar frameworks apply, we rely on one
            or more of the following legal bases:
          </p>
          <ul>
            <li><strong>Consent</strong>: e.g., for optional analytics, marketing communications, and processing you specifically authorize. You may withdraw consent at any time.</li>
            <li><strong>Performance of a contract</strong>: to deliver the Service you sign up for.</li>
            <li><strong>Legitimate interests</strong>: to secure, improve, and operate the Service, provided these interests are not overridden by your rights.</li>
            <li><strong>Legal obligation</strong>: to comply with applicable laws, regulations, and lawful requests.</li>
            <li><strong>Other applicable bases</strong>: such as protecting vital interests or establishing, exercising, or defending legal claims, where relevant.</li>
          </ul>
        </section>

        <section id="cookies">
          <h2>7. Cookies, analytics, SDKs, advertising &amp; tracking technologies</h2>
          <p>
            We and our providers use cookies, local storage, software development kits
            (SDKs), and similar technologies to keep you signed in, remember preferences,
            measure usage, and improve the Service. These may include:
          </p>
          <ul>
            <li><strong>Strictly necessary</strong>: required for authentication, security, and core functionality.</li>
            <li><strong>Analytics &amp; performance</strong>: to understand how the Service is used ([Third-Party Services]).</li>
            <li><strong>Advertising</strong>: only where applicable; we do not sell your personal information for advertising except as described in Section 21.</li>
          </ul>
          <p>
            Where required by law, we request your consent before setting non-essential
            cookies and let you manage your preferences. You can also control cookies through
            your browser settings.
          </p>
        </section>

        <section id="account">
          <h2>8. Account registration &amp; authentication data</h2>
          <p>
            To use the Service you create an account. We process your credentials,
            authentication tokens, and connected-device/session records to sign you in, keep
            your session secure, and let you review and revoke active sessions. We never store
            your password in plain text.
          </p>
        </section>

        <section id="device">
          <h2>9. Device data, usage data, log data &amp; crash reports</h2>
          <p>
            When you use the Service, we automatically collect technical information such as
            device and browser type, operating system, IP address, approximate region derived
            from IP, timestamps, referring pages, actions taken, and error/crash diagnostics.
            We use this data to operate, secure, troubleshoot, and improve the Service.
          </p>
        </section>

        <section id="location">
          <h2>10. Location data</h2>
          <p>
            We may infer your approximate location from your IP address to provide relevant
            job matches and for security. We do not collect precise GPS location unless you
            explicitly grant permission, and you can disable this at any time in your device
            settings.
          </p>
        </section>

        <section id="comms">
          <h2>11. Push notifications, email &amp; in-app communications</h2>
          <p>
            With your permission, we send push notifications, emails, and in-app messages,
            for example, job alerts, application updates, and account notices. You can opt out
            of non-essential communications at any time via your account settings, the
            unsubscribe link in emails, or your device settings. We may still send essential
            service and security messages.
          </p>
        </section>

        <section id="payments">
          <h2>12. Payments, subscriptions &amp; billing</h2>
          <p>
            Where the Service offers paid plans, payments are processed by third-party payment
            providers ([Third-Party Services]). We receive limited billing information (such as
            plan, transaction status, and the last digits of your card) but do not store full
            payment-card numbers on our servers. Your payment data is handled according to the
            payment provider’s privacy policy.
          </p>
        </section>

        <section id="ai">
          <h2>13. AI features, automation &amp; external API integrations</h2>
          <p>
            The Service uses artificial-intelligence features to suggest résumé edits, offer
            cover letter ideas and talking points for application questions, and help you fill
            forms. To provide these
            features, relevant content you supply (such as résumé text and job descriptions)
            may be processed by AI models and external APIs ([Third-Party Services]). We limit
            what is shared to what is needed to perform the requested task. Automated
            processing that produces meaningful output about you is done to deliver features
            you request; where required by law, you can ask for human review of, or object to,
            solely automated decisions that produce legal or similarly significant effects.
          </p>
        </section>

        <section id="sharing">
          <h2>14. How we share personal information</h2>
          <p>We share personal information only as described below:</p>
          <ul>
            <li><strong>Service providers / processors</strong>: hosting, database, analytics, email, payment, and AI providers who process data on our behalf under contract ([Third-Party Services]).</li>
            <li><strong>Affiliates and partners</strong>: where relevant to operate the Service, under appropriate safeguards.</li>
            <li><strong>At your direction</strong>: such as job boards and employers when you submit or auto-fill an application.</li>
            <li><strong>Legal &amp; safety</strong>: to comply with law, respond to lawful requests, enforce our terms, or protect rights, property, and safety.</li>
            <li><strong>Business transfers</strong>: in connection with a merger, acquisition, or sale of assets, subject to this policy.</li>
          </ul>
          <p>We do not sell your personal information except as described in Section 21.</p>
        </section>

        <section id="transfers">
          <h2>15. Cross-border data transfers</h2>
          <p>
            We may store and process personal information in the United States and other
            countries where we or our providers operate (the United States). This means
            your information may be transferred outside Quebec, the rest of Canada, the EEA, the
            UK, or your home region. When we transfer personal information across borders, we
            use appropriate safeguards required by applicable law, such as the European
            Commission’s Standard Contractual Clauses, the UK International Data Transfer
            Agreement/Addendum, adequacy decisions, or, for Quebec, a privacy impact assessment
            confirming adequate protection. Contact us for more information about these
            safeguards.
          </p>
        </section>

        <section id="retention">
          <h2>16. Data retention &amp; destruction</h2>
          <p>
            We keep personal information only as long as necessary for the purposes described
            in this policy, to provide the Service, to comply with legal, accounting, or
            reporting obligations, and to resolve disputes. When information is no longer
            needed, we securely delete, destroy, or anonymize it. You can ask us to delete your
            account and associated data as described in Section 18.
          </p>
        </section>

        <section id="security">
          <h2>17. Security measures</h2>
          <p>
            We use administrative, technical, and physical safeguards designed to protect
            personal information, including encryption in transit, access controls, and secure
            authentication. No method of transmission or storage is completely secure, so we
            cannot guarantee absolute security, but we work to protect your information and to
            respond promptly to any incident.
          </p>
        </section>

        <section id="rights">
          <h2>18. Your privacy rights</h2>
          <p>
            Depending on where you live, you may have some or all of the following rights
            regarding your personal information:
          </p>
          <ul>
            <li><strong>Access</strong>: obtain a copy of the information we hold about you.</li>
            <li><strong>Correction / rectification</strong>: fix inaccurate or incomplete information.</li>
            <li><strong>Deletion / erasure</strong>: ask us to delete your information.</li>
            <li><strong>Portability</strong>: receive your information in a structured, machine-readable format.</li>
            <li><strong>Restriction</strong>: limit how we process your information.</li>
            <li><strong>Objection</strong>: object to certain processing, including direct marketing.</li>
            <li><strong>Withdraw consent</strong>: where processing is based on consent, at any time.</li>
            <li><strong>Complaint</strong>: lodge a complaint with your local data protection authority.</li>
          </ul>
          <p>
            To exercise any right, contact us at <a href="mailto:support@tailrd.ca">support@tailrd.ca</a>. We
            will verify your identity and respond within the timeframe required by applicable
            law. You will not be discriminated against for exercising your rights.
          </p>
        </section>

        <section id="quebec">
          <h2>19. Quebec-specific rights &amp; Law 25 disclosures</h2>
          <p>If you are in Quebec, the following additional disclosures apply:</p>
          <ul>
            <li>Our Privacy Officer (the person in charge of protecting personal information) is Wissam Elmasry, reachable at support@tailrd.ca.</li>
            <li>You have the right to access, correct, and request the deletion of your personal information, and to withdraw consent.</li>
            <li>You have the right to <strong>data portability</strong>, meaning you can receive computerized personal information you provided us in a structured, commonly used technological format.</li>
            <li>Where we use technology to identify, locate, or profile you, we inform you and offer available means to activate or deactivate those functions.</li>
            <li>We conduct privacy impact assessments before transferring personal information outside Quebec (the United States) and before certain new uses.</li>
            <li>You may file a complaint with the <em>Commission d’accès à l’information du Québec</em>.</li>
          </ul>
        </section>

        <section id="gdpr">
          <h2>20. GDPR / UK GDPR rights (EEA &amp; UK)</h2>
          <p>
            If you are in the EEA or the UK, you have the rights listed in Section 18, and you
            may exercise them free of charge in most cases. Our legal bases are described in
            Section 6. You have the right to lodge a complaint with your local supervisory
            authority (for example, your national data protection authority in the EEA, or the
            Information Commissioner’s Office in the UK). Where we transfer data outside the
            EEA/UK, we rely on the safeguards in Section 15.
          </p>
        </section>

        <section id="ccpa">
          <h2>21. California rights (CCPA / CPRA)</h2>
          <p>
            If you are a California resident, you have the right to know what personal
            information we collect, use, and disclose; to request deletion; to correct
            inaccurate information; and to opt out of the “sale” or “sharing” of personal
            information and of certain uses of sensitive personal information, as those terms
            are defined under the CCPA/CPRA. We do not sell your personal information for money.
            We will not discriminate against you for exercising your rights. To make a request,
            contact us at support@tailrd.ca; you may use an authorized agent where permitted.
          </p>
        </section>

        <section id="children">
          <h2>22. Children's privacy</h2>
          <p>
            The Service is not directed to children and is intended for users of at least the
            age of majority in their jurisdiction (or, at minimum, 16, or the age required by
            local law). We do not knowingly collect personal information from children below
            the applicable age. If you believe a child has provided us personal information,
            contact us at support@tailrd.ca and we will delete it.
          </p>
        </section>

        <section id="breach">
          <h2>23. Breach notification &amp; incident response</h2>
          <p>
            We maintain procedures to detect, investigate, and respond to security incidents.
            If a breach affecting your personal information occurs and poses a risk of serious
            harm, we will notify affected individuals and the relevant authorities as required
            by applicable law, including Quebec’s confidentiality-incident rules, the
            GDPR/UK GDPR’s 72-hour notification requirement, and applicable Canadian and U.S.
            breach-notification laws.
          </p>
        </section>

        <section id="changes">
          <h2>24. Changes to this Privacy Policy</h2>
          <p>
            We may update this policy from time to time. When we make material changes, we
            will update the “Last updated” date above and, where required, notify you through
            the Service or by email. Your continued use of the Service after an update takes
            effect constitutes acceptance of the revised policy.
          </p>
        </section>

        <section id="contact">
          <h2>25. Contact us &amp; privacy requests</h2>
          <p>
            For any question, request, or complaint about this policy or your personal
            information, contact:
          </p>
          <ul>
            <li><strong>Tailrd</strong></li>
            <li><strong>Privacy Officer:</strong> Wissam Elmasry</li>
            <li><strong>Email:</strong> support@tailrd.ca</li>
          </ul>
          <p>
            We will acknowledge and respond to your request within the time required by
            applicable law.
          </p>
        </section>

        <p className="legal-footer-link">
          <Link to="/">← Back to home</Link>
        </p>
      </div>
    </div>
  );
}
