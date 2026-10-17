/**
 * Every screen the audit visits. A "state" is a route plus the interactions
 * needed to reach a view that a user actually sees, an open modal counts as a
 * screen, so it gets audited like one.
 */

const AUTHED = { authed: true };
const ANON = { authed: false };

/* Setup wizard: each step gates on its own validate(), so reaching a later step
 * means genuinely satisfying the earlier ones, the path a new user walks. */
const NEXT = "button.setup-btn";
const SETUP_NAME = [
  { fill: ".setup-input", nth: 0, value: "Wissam" },
  { fill: ".setup-input", nth: 1, value: "Elmasry" },
  { click: NEXT },
];
const SETUP_ROLE = [
  { click: ".setup-checkgrid .setup-check", nth: 0 }, // a job function
  { selectIndex: ".setup-select", index: 1 },         // a country (skip the placeholder)
  { click: NEXT },
];
const SETUP_EXPERIENCE = [
  { click: ".setup-checkgrid .setup-check", nth: 0 },
  { click: NEXT },
];

/** @type {{id:string, url:string, authed?:boolean, user?:object, empty?:boolean, wait?:string, steps?:Array}[]} */
const STATES = [
  /* ---------------- public / marketing ---------------- */
  { id: "landing", url: "/", ...ANON, wait: ".site-header, header, body" },
  { id: "about", url: "/about", ...ANON },
  { id: "pricing", url: "/pricing", ...ANON },
  { id: "privacy", url: "/privacy", ...ANON },
  { id: "terms", url: "/terms", ...ANON },
  { id: "cookies", url: "/cookies", ...ANON },
  { id: "support", url: "/support", ...ANON },
  { id: "sign-in", url: "/sign-in", ...ANON },
  { id: "sign-up", url: "/sign-up", ...ANON },
  { id: "verify-email", url: "/verify-email", ...AUTHED, user: { email_verified: false } },
  { id: "linkedin-complete", url: "/linkedin/complete", ...ANON },
  { id: "extension-connect", url: "/extension/connect?state=abc&code_challenge=xyz", ...AUTHED },
  { id: "jobs-list-public", url: "/list", ...ANON },
  { id: "demo-apply", url: "/demo-apply", ...ANON },

  /* ---------------- setup wizard, ALL FOUR steps ----------------
     Only the first two were covered before, which is how a broken `role` step
     shipped. Each step gates on its own validate(), so reaching step N means
     satisfying steps 1..N-1 for real, the same path a new user walks. */
  {
    id: "setup-1-welcome",
    url: "/setup",
    ...AUTHED,
    user: { has_completed_setup: false },
    wait: ".setup-right",
  },
  {
    id: "setup-2-role",
    url: "/setup",
    ...AUTHED,
    user: { has_completed_setup: false },
    wait: ".setup-right",
    steps: [...SETUP_NAME, { waitFor: ".setup-checkgrid" }],
  },
  {
    // Experience level + job type now share one step; the target-titles step is gone.
    id: "setup-3-experience",
    url: "/setup",
    ...AUTHED,
    user: { has_completed_setup: false },
    wait: ".setup-right",
    steps: [...SETUP_NAME, ...SETUP_ROLE, { waitFor: ".setup-check" }],
  },
  {
    id: "setup-4-resume",
    url: "/setup",
    ...AUTHED,
    user: { has_completed_setup: false },
    wait: ".setup-right",
    steps: [
      ...SETUP_NAME, ...SETUP_ROLE, ...SETUP_EXPERIENCE,
      { waitFor: ".setup-resume" },
    ],
  },

  /* ---------------- embedded (extension iframes) ----------------
     These pages wait for the extension to hand them a token + job over a
     MessageChannel. `embedBridge` makes the harness play the extension's part;
     without it they render a spinner forever. */
  { id: "embed-custom-resume", url: "/embed/custom-resume", ...ANON, embedBridge: true, wait: ".ai-modal" },
  { id: "embed-cover-letter", url: "/embed/cover-letter", ...ANON, embedBridge: true, wait: ".ai-modal, .cl-modal" },

  /* ---------------- app shell ---------------- */
  { id: "app-dashboard", url: "/app", ...AUTHED, wait: ".jobs-page" },
  {
    id: "app-dashboard-sidebar-collapsed",
    url: "/app",
    ...AUTHED,
    wait: ".jobs-page",
    // 768–1023 is already a permanent icon rail, so the collapse toggle is
    // hidden there too. It only exists on the full 240px sidebar at ≥1024.
    minWidth: 1024,
    steps: [{ click: ".sidebar-collapse-btn" }],
  },
  {
    id: "app-nav-drawer-open",
    url: "/app",
    ...AUTHED,
    wait: ".app-topbar-menu",
    maxWidth: 767, // the drawer only exists on phones
    steps: [{ click: ".app-topbar-menu" }, { waitFor: ".sidebar-backdrop" }],
  },
  {
    id: "app-dashboard-filters-open",
    url: "/app",
    ...AUTHED,
    wait: ".jobs-page",
    // Filters are always visible; open the first pill's dropdown instead.
    steps: [{ click: '[aria-label="Job filters"] button' }],
  },
  {
    id: "app-job-detail",
    url: "/app",
    ...AUTHED,
    wait: ".job-card-body",
    steps: [{ click: ".job-card-body" }, { waitFor: ".job-detail-view, .job-detail-inline" }],
  },
  // Both of these live behind a sidebar nav item. Under 768px the sidebar is an
  // off-canvas drawer, so the drawer has to be opened first, otherwise the
  // click misses, the modal never opens, and the audit measures the dashboard
  // underneath and calls it clean. `clickIfVisible` is a no-op on desktop,
  // where the hamburger does not exist.
  {
    id: "app-refer-modal",
    url: "/app",
    ...AUTHED,
    wait: ".jobs-page",
    steps: [
      { clickIfVisible: ".app-topbar-menu" },
      { click: "button.nav-item:has-text('Refer & Earn')" },
      { waitFor: ".refer-modal" },
    ],
  },
  {
    id: "app-settings-modal",
    url: "/app",
    ...AUTHED,
    wait: ".jobs-page",
    steps: [
      { clickIfVisible: ".app-topbar-menu" },
      { click: "button.nav-item:has-text('Settings')" },
      { waitFor: ".settings-modal, .sm-panel, .modal-overlay" },
    ],
  },
  // ?extState= is ExtensionBanner's dev-only override. The extension's
  // externally_connectable covers tailrd.ca only, so the real ping can never
  // answer from localhost, and the audit runs against `npm run dev`, where
  // import.meta.env.DEV is true and the override is compiled in.
  // `wait` is swallowed on timeout, so a banner that never renders would leave the
  // audit measuring the bare dashboard and reporting it clean, a false all-clear,
  // and the banner has three independent ways to silently stop rendering (the
  // override, the ping, the snooze). `steps[].waitFor` is the loud form: it emits a
  // high-severity `state-not-reached` finding instead.
  {
    id: "app-extension-banner",
    url: "/app?extState=not-installed",
    ...AUTHED,
    wait: ".jobs-page",
    steps: [{ waitFor: ".ext-banner" }],
  },

  /* ---------------- in-app AI modals (same CSS as the /embed pages, but
     rendered over the dashboard rather than in an extension iframe) --------- */
  {
    id: "app-ai-resume-modal",
    url: "/app",
    ...AUTHED,
    wait: ".job-card-body",
    steps: [{ click: "button.btn-ai:has-text('Custom Resume')" }, { waitFor: ".ai-modal" }],
  },
  {
    id: "app-ai-cover-modal",
    url: "/app",
    ...AUTHED,
    wait: ".job-card-body",
    steps: [{ click: "button.btn-ai:has-text('Cover Letter')" }, { waitFor: ".ai-modal, .cl-modal" }],
  },

  /* ---------------- first-run onboarding tour ----------------
     Gated purely on user.has_completed_onboarding (OnboardingProvider). The
     waitFor is the point: without it, a tour that silently fails to start would
     just audit the bare dashboard and report clean. */
  {
    id: "app-onboarding-tour",
    url: "/app",
    ...AUTHED,
    user: { has_completed_onboarding: false },
    wait: ".jobs-page",
    steps: [{ waitFor: ".tour-overlay" }],
  },

  /* ---------------- resume ---------------- */
  { id: "app-resume-list", url: "/app/resume", ...AUTHED, wait: ".resume-page-new" },
  {
    id: "app-resume-upload-modal",
    url: "/app/resume",
    ...AUTHED,
    wait: ".resume-add-btn",
    steps: [{ click: ".resume-add-btn" }, { waitFor: ".upload-modal-new" }],
  },
  { id: "app-resume-detail-edit", url: "/app/resume/1", ...AUTHED, wait: ".rd-workspace" },
  {
    id: "app-resume-detail-preview",
    url: "/app/resume/1",
    ...AUTHED,
    wait: ".rd-workspace",
    // From 1024px up both panes fit side by side and the toggle is hidden, the
    // preview is on screen either way, so the click is a no-op there by design.
    steps: [{ clickIfVisible: ".rd-pane-toggle button:has-text('Preview')" }],
  },
  {
    id: "app-resume-detail-report",
    url: "/app/resume/1",
    ...AUTHED,
    wait: ".rd-score-actions button",
    steps: [{ click: "button:has-text('View full report')" }, { waitFor: ".rd-report, .report, main" }],
  },

  /* ---------------- profile / applications / interview / feedback ---------------- */
  { id: "app-profile", url: "/app/profile", ...AUTHED, wait: ".profile-page, main" },
  { id: "app-applications", url: "/app/applications", ...AUTHED, wait: ".jobs-page" },
  { id: "app-applications-empty", url: "/app/applications", ...AUTHED, empty: true, wait: ".empty-state" },
  { id: "app-interview", url: "/app/interview", ...AUTHED, wait: ".interview-page" },
  {
    id: "app-interview-company",
    url: "/app/interview",
    ...AUTHED,
    wait: ".interview-company-card",
    steps: [{ click: ".interview-company-card" }, { waitFor: ".interview-questions-list, .interview-company-header" }],
  },
  {
    id: "app-interview-request-modal",
    url: "/app/interview",
    ...AUTHED,
    wait: ".interview-request-cta",
    steps: [{ click: ".interview-request-cta" }, { waitFor: ".interview-request-modal" }],
  },
  { id: "app-feedback", url: "/app/feedback", ...AUTHED, wait: ".feedback-page, main" },
  { id: "app-refer-page", url: "/app/refer", ...AUTHED, wait: "main" },

  /* Internal feedback console. Not part of the product, but it renders text a
     user typed, so it wraps or it does not like any other screen. */
  { id: "admin-feedback", url: "/admin", ...AUTHED, wait: ".admin-list, .admin-empty" },
];

/** Device matrix. `touch` widths get the tap-target detector. */
const VIEWPORTS = [
  { id: "320x568", w: 320, h: 568, touch: true, label: "iPhone SE (smallest realistic phone)" },
  { id: "360x740", w: 360, h: 740, touch: true, label: "Android (most common phone width)" },
  { id: "390x844", w: 390, h: 844, touch: true, label: "iPhone 14/15" },
  { id: "430x932", w: 430, h: 932, touch: true, label: "iPhone Pro Max" },
  { id: "600x960", w: 600, h: 960, touch: true, label: "small tablet / phone landscape" },
  { id: "768x1024", w: 768, h: 1024, touch: true, label: "iPad portrait" },
  { id: "834x1112", w: 834, h: 1112, touch: true, label: "iPad Air portrait" },
  { id: "1024x768", w: 1024, h: 768, touch: false, label: "iPad landscape / netbook" },
  { id: "1280x720", w: 1280, h: 720, touch: false, label: "small laptop" },
  { id: "1366x640", w: 1366, h: 640, touch: false, label: "laptop, short viewport (browser chrome + dock)" },
  { id: "1440x900", w: 1440, h: 900, touch: false, label: "MacBook" },
  { id: "1920x1080", w: 1920, h: 1080, touch: false, label: "desktop full screen" },
  { id: "2560x1440", w: 2560, h: 1440, touch: false, label: "large monitor" },
];

module.exports = { STATES, VIEWPORTS };
