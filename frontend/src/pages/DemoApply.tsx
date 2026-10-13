import OnboardingWizard from "../components/OnboardingWizard";
import { Logo } from "../components/brand";
import "./DemoApply.css";

/**
 * Greenhouse-style demo job application page.
 * Renders a realistic form + the OnboardingWizard overlay.
 * Accessible at /demo-apply without authentication.
 */
export default function DemoApply() {
  return (
    <div className="demo-apply-page">
      {/* Company Header */}
      <header className="demo-apply-header">
        <div className="demo-apply-header-inner">
          <Logo variant="mark" theme="light" size={48} className="demo-apply-logo" />
          <div className="demo-apply-header-text">
            <h1 className="demo-apply-company">Tailrd</h1>
            <p className="demo-apply-job-title">Software Engineer, Full Stack</p>
          </div>
        </div>
      </header>

      {/* Main Form Area */}
      <main className="demo-apply-main">
        <div className="demo-apply-card">
          <h2 className="demo-apply-form-heading">Submit Your Application</h2>
          <p className="demo-apply-form-subheading">
            Fields marked with <span className="demo-required-star">*</span> are required.
          </p>

          {/* Autofill Button Area */}
          <div className="demo-apply-autofill-bar">
            <button
              id="autofill-btn"
              type="button"
              className="demo-autofill-btn"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <path d="M13.5 2.5L6.5 9.5L2.5 5.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Fill from my profile
            </button>
          </div>

          {/* Form Fields */}
          <form id="demo-form-fields" className="demo-apply-form" onSubmit={(e) => e.preventDefault()}>
            <div className="demo-form-row">
              <div className="demo-form-field">
                <label htmlFor="demo-first-name" className="demo-form-label">
                  First Name <span className="demo-required-star">*</span>
                </label>
                <input
                  id="demo-first-name"
                  type="text"
                  className="demo-form-input"
                  placeholder="First Name"
                  autoComplete="given-name"
                />
              </div>
              <div className="demo-form-field">
                <label htmlFor="demo-last-name" className="demo-form-label">
                  Last Name <span className="demo-required-star">*</span>
                </label>
                <input
                  id="demo-last-name"
                  type="text"
                  className="demo-form-input"
                  placeholder="Last Name"
                  autoComplete="family-name"
                />
              </div>
            </div>

            <div className="demo-form-field">
              <label htmlFor="demo-email" className="demo-form-label">
                Email <span className="demo-required-star">*</span>
              </label>
              <input
                id="demo-email"
                type="email"
                className="demo-form-input"
                placeholder="you@example.com"
                autoComplete="email"
              />
            </div>

            <div className="demo-form-field">
              <label htmlFor="demo-phone" className="demo-form-label">
                Phone <span className="demo-required-star">*</span>
              </label>
              <input
                id="demo-phone"
                type="tel"
                className="demo-form-input"
                placeholder="(555) 123-4567"
                autoComplete="tel"
              />
            </div>

            <div className="demo-form-field">
              <label htmlFor="demo-linkedin" className="demo-form-label">
                LinkedIn Profile
              </label>
              <input
                id="demo-linkedin"
                type="url"
                className="demo-form-input"
                placeholder="https://linkedin.com/in/yourprofile"
                autoComplete="url"
              />
            </div>

            {/* Custom Question Textarea */}
            <div className="demo-form-field">
              <label htmlFor="custom-question-textarea" className="demo-form-label">
                Why are you a good fit for Tailrd? <span className="demo-required-star">*</span>
              </label>
              <textarea
                id="custom-question-textarea"
                className="demo-form-textarea"
                placeholder="Tell us why you'd be a great addition to the team..."
                rows={5}
              />
            </div>

            {/* Resume Upload */}
            <div className="demo-form-field">
              <label htmlFor="demo-resume-upload" className="demo-form-label">
                Resume <span className="demo-required-star">*</span>
              </label>
              <div className="demo-resume-upload-area">
                <input
                  id="demo-resume-upload"
                  type="file"
                  className="demo-resume-file-input"
                  accept=".pdf,.doc,.docx"
                />
                <div className="demo-resume-upload-placeholder">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path d="M12 16V4M12 4L8 8M12 4L16 8M4 20H20" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                  <span>Drag & drop or click to upload (PDF, DOC, DOCX)</span>
                </div>
              </div>
            </div>

            {/* Generate Resume Button */}
            <div className="demo-form-field">
              <button
                id="generate-resume-btn"
                type="button"
                className="demo-generate-resume-btn"
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                  <path d="M8 1V15M1 8H15" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
                </svg>
                Suggest edits for this job
              </button>
            </div>

            {/* Extension Popup Area */}
            <div id="extension-popup-area" className="demo-extension-popup-area">
              <div className="demo-extension-popup-header">
                <Logo variant="mark" theme="light" size={24} decorative className="demo-extension-popup-logo" />
                <span className="demo-extension-popup-title">Tailrd Extension</span>
              </div>
              <p className="demo-extension-popup-text">
                Résumé suggestions and cover letter ideas are available here.
              </p>
            </div>

            {/* Autofill Info Section */}
            <div id="autofill-info-section" className="demo-autofill-info-section">
              <h3 className="demo-autofill-info-heading">Your profile on this device</h3>
              <div className="demo-autofill-info-items">
                <div className="demo-autofill-info-item">
                  <span className="demo-autofill-info-label">Name</span>
                  <span className="demo-autofill-info-value">Click to copy</span>
                </div>
                <div className="demo-autofill-info-item">
                  <span className="demo-autofill-info-label">Email</span>
                  <span className="demo-autofill-info-value">Click to copy</span>
                </div>
                <div className="demo-autofill-info-item">
                  <span className="demo-autofill-info-label">Phone</span>
                  <span className="demo-autofill-info-value">Click to copy</span>
                </div>
              </div>
            </div>

            {/* Submit Button */}
            <button
              id="demo-submit-btn"
              type="submit"
              className="demo-submit-btn"
              disabled
            >
              Submit Application
            </button>
          </form>
        </div>
      </main>

      {/* Onboarding Wizard Overlay */}
      <OnboardingWizard />
    </div>
  );
}
