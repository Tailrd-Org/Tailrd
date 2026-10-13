/**
 * Shared types used across the popup, content script, background worker
 * and API client.
 */

// ---------------------------------------------------------------------------
// User application profile
// ---------------------------------------------------------------------------

export interface EducationEntry {
  school: string;
  degree: string;
  graduationYear: string;
}

export interface ExperienceEntry {
  company: string;
  title: string;
  startDate: string;
  endDate: string;
  description: string;
}

/**
 * Optional EEO / demographic answers. These are ONLY used when the user
 * explicitly enables "Fill EEO fields" in the extension settings.
 */
export interface EeoAnswers {
  gender?: string;
  genderIdentity?: string;
  pronouns?: string;
  race?: string;
  hispanicLatino?: string;
  veteranStatus?: string;
  disabilityStatus?: string;
  sexualOrientation?: string;
}

/**
 * The canonical profile shape the extension fills from.
 * Mirrors GET /api/user/application-profile (see src/api/client.ts).
 */
export interface UserApplicationProfile {
  firstName: string;
  lastName: string;
  email: string;
  phone: string;
  location: string;
  /** Structured mailing address (all default "" server-side; see Task C1). */
  addressStreet: string;
  addressCity: string;
  addressState: string;
  postalCode: string;
  country: string;
  linkedin: string;
  github: string;
  portfolio: string;
  currentCompany: string;
  currentTitle: string;
  workAuthorization: string;
  requiresSponsorship: string;
  /**
   * ISO "YYYY-MM-DD". The one profile fact that exists purely so an age gate
   * ("Are you 18 or older?") is COMPUTED rather than guessed, see
   * backend/services/derived_facts.py. Blank is normal and means every age
   * resolver abstains, which is the honest default, not a degraded one.
   */
  dateOfBirth: string;
  /**
   * Screening answers the applicant stated once on their profile (2026-08-09
   * profile-parity contract). Each is a FACT the user supplied, so a matching
   * question is answered from it directly, pass 1, no backend/LLM call. Blank
   * (or absent) means "not answered" and every resolver abstains.
   */
  willingToRelocate?: string;
  workPreference?: string;
  noticePeriod?: string;
  earliestStartDate?: string;
  yearsOfExperience?: string;
  securityClearance?: string;
  driversLicense?: string;
  languages?: string;
  /** "How did you hear about us?": the channel the applicant names by default
   *  (LinkedIn | Job board | Company website | Referral | Career fair | Social
   *  media | Other). Blank means "Job board", the truthful default for a
   *  Tailrd user (see content/defaultAnswers.ts). */
  howDidYouHear?: string;
  /** Explicit "Authorized to work in the US / in Canada": "Yes" | "No". Each
   *  answers exactly the employer's question for that country and wins over
   *  the free-text workAuthorization reading (profileFacts). */
  authorizedUS?: string;
  authorizedCanada?: string;
  /** Expected graduation MONTH, "YYYY-MM": answers season / term / month-range
   *  graduation options that a year alone fits several of. */
  expectedGraduation?: string;
  /** "3.7/4.0", "3.7", "85%". */
  gpa?: string;
  education: EducationEntry[];
  experience: ExperienceEntry[];
  skills: string[];
  coverLetter: string;
  salaryExpectation?: string;
  eeo?: EeoAnswers;
}

/** Where a profile came from, shown in the popup so the user always knows. */
export type ProfileSource = "api" | "api-settings" | "cache" | "mock";

/** A resume available for syncing / auto-upload (mirrors a sync snapshot item). */
export interface ResumeSummary {
  id: number;
  name: string;
  isPrimary: boolean;
  /** True when the original PDF/DOCX is stored and can be auto-uploaded. */
  hasFile: boolean;
  fileName?: string;
  fileContentType?: string;
  updatedAt?: string | null;
}

/** A saved cover letter (mirrors a sync snapshot item). */
export interface CoverLetterSummary {
  id: number;
  jobTitle: string;
  company: string;
  text: string;
  tone: string;
  isActive: boolean;
  updatedAt?: string | null;
}

/** A job-specific / AI / user resume version (mirrors a sync snapshot item). */
export interface CustomResumeSummary {
  id: number;
  label: string;
  jobId: number | null;
  source: string;
  createdAt?: string | null;
}

/** Subscription status: forward-compatible stub until billing exists. */
export interface Subscription {
  tier: string;
  status: string;
}

/** Usage limits: forward-compatible stub until metering exists. */
export interface Usage {
  aiCreditsUsed: number;
  aiCreditsLimit: number | null;
}

/** Server-side settings the extension cares about. */
export interface SyncSettings {
  jobTitle: string;
  prefilledAnswers: Record<string, string>;
}

/**
 * One-shot snapshot of everything the extension syncs from the web app.
 * Mirrors GET /api/extension/sync. The web app is the source of truth.
 */
export interface ExtensionSyncSnapshot {
  version: number;
  updatedAt: string | null;
  profile: UserApplicationProfile;
  resumes: ResumeSummary[];
  activeResumeId: number | null;
  coverLetters: CoverLetterSummary[];
  customResumes: CustomResumeSummary[];
  settings: SyncSettings;
  subscription: Subscription;
  usage: Usage;
}

// ---------------------------------------------------------------------------
// Field detection
// ---------------------------------------------------------------------------

export type FieldCategory =
  | "firstName"
  | "lastName"
  | "fullName"
  | "email"
  | "phone"
  // Workday splits a phone number into three controls: the dialing country
  // ("Canada (+1)"), the number, and the device type ("Mobile"). The two
  // satellites are dropdowns, NOT text, routing the number into either fails.
  | "phoneCountryCode"
  | "phoneDeviceType"
  | "location"
  | "addressStreet"
  | "addressCity"
  | "addressState"
  | "postalCode"
  | "country"
  | "linkedin"
  | "github"
  | "portfolio"
  | "resumeUpload"
  | "coverLetter"
  | "workAuthorization"
  | "sponsorship"
  | "education"
  | "school"
  | "degree"
  // Workday renders Degree and Field of Study as two separate typeaheads. The
  // subject dropdown rejects the degree's own text, so it needs its own category.
  | "fieldOfStudy"
  | "graduationYear"
  | "experience"
  | "currentCompany"
  | "currentTitle"
  | "experienceStartDate"
  | "experienceEndDate"
  | "experienceDescription"
  | "experienceCurrent"
  | "salary"
  | "skills"
  // Screening answers held on the profile (2026-08-09 profile-parity contract).
  // Each resolves straight from a stated fact, so these questions cost no LLM
  // call. `startDate` is the AVAILABILITY date ("when can you start?") and is
  // deliberately distinct from experienceStartDate, which only ever resolves
  // inside a repeating employment row.
  | "willingToRelocate"
  | "workPreference"
  | "noticePeriod"
  | "startDate"
  | "yearsOfExperience"
  | "securityClearance"
  | "driversLicense"
  | "languages"
  // EEO / demographic: detected but never autofilled unless explicitly enabled
  | "eeoGender"
  | "eeoGenderIdentity"
  | "eeoPronouns"
  | "eeoRace"
  | "eeoHispanic"
  | "eeoVeteran"
  | "eeoDisability"
  | "eeoSexualOrientation"
  | "eeoOther"
  // The date beside a signature ("Date" under the disability self-ID form's
  // Name, Lever's eeo[disabilitySignatureDate]; "Today's date"): today.
  | "signatureDate"
  // Account signup password: first-class but locked down: scanned so the
  // account sub-flow can fill it, but never generically fillable or AI-eligible.
  | "accountPassword"
  | "unknown";

export type ControlType =
  | "text"
  | "textarea"
  | "select"
  | "checkbox"
  | "radioGroup"
  | "checkboxGroup"
  | "file"
  | "contenteditable"
  | "combobox"
  | "ariaRadioGroup"
  | "customDropdown"
  // Account signup password: never generically fillable or AI-eligible;
  // written/verified only by the account sub-flow.
  | "password";

/** The control shape the resolver + adapters read when resolving/overriding an answer. */
export interface ResolveControl {
  controlType: ControlType;
  options?: string[];
  groupIndex?: number | null;
}

/**
 * Serializable summary of a detected form field, sent from the content
 * script to the popup for review before any filling happens.
 */
export interface DetectedField {
  /** Stable id, prefixed with the owning frame's token (e.g. "x7ab2-3"). */
  id: string;
  category: FieldCategory;
  /** 0..1, how confident the matcher is about the category. */
  confidence: number;
  /** Best human-readable label found for the field. */
  label: string;
  controlType: ControlType;
  required: boolean;
  /** Value resolved from the profile; null when we have nothing to fill. */
  proposedValue: string | null;
  /** Whether the autofill engine can write this control at all. */
  fillable: boolean;
  /** EEO / demographic flag: excluded from autofill unless enabled. */
  sensitive: boolean;
  /** Short note shown in the review panel (e.g. file-upload guidance). */
  note?: string;
  /** Available options for selects / radio groups (trimmed for display). */
  options?: string[];
  /** Current value already present in the control, if any. */
  currentValue?: string;
  /** Surrounding help/section text (FieldSignals.nearby), page context for the AI. */
  helpText?: string;
  /** Native input type hint (FieldSignals.typeHint: "date", "number"…), AI format cue. */
  inputType?: string;
  /** 0-based repeating-row index parsed from the field name (`experience[1][company]`),
   *  or null/undefined when the field is not part of a repeating section. */
  groupIndex?: number | null;
  /** The proposed value was COMPUTED from profile facts by the deterministic
   *  question resolver (fieldResolver.ts): fill it on device, no backend. */
  deterministic?: boolean;
  /** The device recognized this question and decided the profile does not
   *  settle it: leave it blank and never send it to the backend's guessers. */
  deviceAbstained?: boolean;
  /** What kind of answer the field accepts (answerKind.ts), for diagnostics. */
  answerKind?: string;
  /** The format a date control displays (content/dateControl.ts); any value
   *  written to it is first fitted to this format, or dropped. */
  dateFormat?: "YYYY-MM-DD" | "MM/DD/YYYY" | "DD/MM/YYYY" | "YYYY-MM" | "MM/YYYY" | "DD Mon YYYY" | "Mon DD, YYYY";
}

// ---------------------------------------------------------------------------
// AI-assisted fill (backend POST /api/fill)
// ---------------------------------------------------------------------------

/** A field handed to the backend AI fill endpoint (mirrors backend FormField). */
export interface AiFillField {
  id: string;
  label: string;
  type: "text" | "textarea" | "select" | "radio" | "checkbox";
  options: string[];
  required: boolean;
  /** Surrounding help/section text: page context for the AI (mirrors FormField.helpText). */
  helpText: string;
  /** Native input type hint: AI format cue (mirrors FormField.inputType). */
  inputType: string;
}

/**
 * Non-sensitive slice of the user's Autofill Information, sent to the backend AI
 * as grounding context so answers come from what the user maintains. EEO /
 * demographic data is deliberately excluded. It never leaves the device.
 * Mirrors backend ApplicantProfile (backend/routers/fill.py). Built by
 * toApplicantProfile (content/applicantProfile.ts).
 */
export interface ApplicantProfile {
  firstName: string;
  lastName: string;
  email: string;
  phone: string;
  location: string;
  addressStreet: string;
  addressCity: string;
  addressState: string;
  postalCode: string;
  country: string;
  linkedin: string;
  github: string;
  portfolio: string;
  currentCompany: string;
  currentTitle: string;
  workAuthorization: string;
  requiresSponsorship: string;
  salaryExpectation: string;
  /** Screening answers stated on the profile, the backend's `_profile_context()`
   *  emits them and `_raw_rule_based_answer` answers the obvious ones without a
   *  model call, so they must reach it (mirrors backend ApplicantProfile). */
  willingToRelocate: string;
  workPreference: string;
  noticePeriod: string;
  earliestStartDate: string;
  yearsOfExperience: string;
  securityClearance: string;
  driversLicense: string;
  languages: string;
  skills: string[];
  experience: string[];
  education: string[];
  /**
   * Facts the backend can COMPUTE an answer from, kept structured so no
   * resolver has to re-parse the display strings above. `experience` and
   * `education` stay as prose because that is what the model reads; arithmetic
   * reads these.
   */
  dateOfBirth: string;
  workHistory: { startDate: string; endDate: string }[];
  educationHistory: { degree: string; school: string; graduationYear: string }[];
}

/** Scraped page context that improves AI answers. Empty strings are fine. */
export interface JobContext {
  jobDescription: string;
  jobTitle: string;
  company: string;
}

/** One answer from the backend (mirrors backend FieldAnswer). */
export interface AiFillAnswer {
  id: string;
  label: string;
  answer: string;
  confidence: string;
  /** rule | profile | ai, how the answer was produced. */
  source?: string;
  /** AI suggestions need user review. */
  needsReview?: boolean;
  category?: string;
  /** Which backend pass produced it: "derived" | "rule" | "ai".
   *  Absent from older backends. */
  fillPass?: string;
}

/**
 * A candidate value the backend's grounding gate refused.
 *
 * The field is left blank on purpose: a value the profile contradicts, or one
 * the widget does not offer, is worse than an empty field, because an empty
 * field is one the post-fill re-scan can still ask the user about. Carries the
 * reason and never the value.
 */
export interface DroppedAnswer {
  id: string;
  label: string;
  /** "no_answer" | "not_an_offered_option" | "contradicts_profile:<what>". */
  reason: string;
  /** Which backend pass proposed it: "derived" | "rule" | "ai". */
  source: string;
}

/** Background-worker reply for an AI_FILL request. */
export interface AiFillResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  answers: AiFillAnswer[];
  errors: string[];
  dropped?: DroppedAnswer[];
}

// ---------------------------------------------------------------------------
// Multi-page autofill flow
// ---------------------------------------------------------------------------

/** Why a flow is waiting on the user (all reasons auto-resume when cleared). */
export type FlowPauseReason =
  | "captcha"
  | "resume-upload"
  | "validation"
  | "account"
  | "verification"
  | "unfilled-required";

export type FlowPhase = "filling" | "advancing" | "paused" | "ready" | "done" | "stopped";

/** Persisted per-tab so a flow survives real navigations (background-owned). */
export interface FlowState {
  active: boolean;
  step: number;
  startedAt: number;
  /** fieldSignature() of the step we just advanced FROM (loop detection). */
  lastSignature: string;
}

/** One progress beat for the panel's flow status line. */
export interface FlowProgress {
  phase: FlowPhase;
  step: number;
  filledOk: number;
  filledFail: number;
  pauseReason?: FlowPauseReason;
  detail?: string;
  /** Text of the button the flow will click next ("Next", "Create Account"…),
   *  labels the panel's manual advance gate to match the real page. */
  nextLabel?: string;
  /** On a "ready" beat: the flow turns the page by itself after this many ms
   *  unless the user holds it, so the panel counts down on its bottom button.
   *  Absent: the gate waits for a press. */
  autoAdvanceMs?: number;
}

/** Flow-owning frame → top-frame panel (via RELAY_TO_TOP). */
export interface RemoteFlowProgress {
  type: "REMOTE_FLOW_PROGRESS";
  progress: FlowProgress;
}

/** Background reply for FLOW_STATE_GET. */
export interface FlowStateResponse {
  ok: boolean;
  state?: FlowState | null;
}

/** The top frame's answer to JOB_PLACE_GET (relayed for TOP_JOB_PLACE_GET):
 *  where the job is, as the page around an embedded form states it. */
export interface JobPlaceResponse {
  ok: boolean;
  country?: string | null;
  city?: string | null;
  company?: string;
}

// ---------------------------------------------------------------------------
// Résumé retailoring (backend POST /api/tailor-resume, /api/render-resume)
// ---------------------------------------------------------------------------

/** Opaque structured résumé document (backend ResumeDocument); passed through. */
export type ResumeDoc = Record<string, unknown>;

/** Options for a tailor request, chosen in the overlay. */
export interface TailorResumeOpts {
  resumeId: number | null;
  sections?: string[];
  /** null/undefined -> weave all missing keywords; a list -> exactly that set. */
  addKeywords?: string[] | null;
}

/** Normalized tailor result the overlay renders (camelCase). Scores are the match engine's
 * and are null when it shows no number (low confidence, needs review, not eligible). */
export interface TailorResult {
  document: ResumeDoc;
  originalScore: number | null;
  newScore: number | null;
  atsScore: number | null;
  keywordCoverage: number | null;
  /** The engine's band ("Good match") or "We couldn't score this" / "Not eligible". */
  matchLabel: string;
  /** Why the tailored result shows no number (plain sentences); empty when it is scored. */
  reasons: string[];
  matchedKeywords: string[];
  missingKeywords: string[];
  diffSummary: string;
}

/** Background reply for TAILOR_RESUME. */
export interface TailorResumeResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  result?: TailorResult;
}

/** Background reply for GET_ACCESS_TOKEN (feeds the /embed/* iframe bridge). */
export interface AccessTokenResponse {
  ok: boolean;
  token: string;
  apiBaseUrl?: string;
  needsLogin?: boolean;
  error?: string;
}

/** Background reply for RENDER_RESUME (mirrors ResumeFileResponse). */
export interface RenderResumeResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  dataBase64?: string;
  name: string;
  contentType: string;
}

// ---------------------------------------------------------------------------
// Cover-letter generation (backend POST /api/cover-letter, /api/render-cover-letter)
// ---------------------------------------------------------------------------

/** Options for a cover-letter generate request, chosen in the overlay. */
export interface CoverLetterGenOpts {
  resumeId: number | null;
  tone?: string | null;
  /** null/undefined -> fresh letter; a string -> rewrite this text in `tone`. */
  baseText?: string | null;
}

/** Background reply for GENERATE_COVER_LETTER. */
export interface GenerateCoverLetterResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  text?: string;
}

/** Background reply for RENDER_COVER_LETTER (mirrors RenderResumeResponse). */
export interface RenderCoverLetterResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  dataBase64?: string;
  name: string;
  contentType: string;
}

// ---------------------------------------------------------------------------
// Popup <-> content script messages
// ---------------------------------------------------------------------------

export type ContentRequest =
  | { type: "PING" }
  | { type: "TOGGLE_PANEL" }
  | { type: "JOB_PLACE_GET" }
  | {
      type: "SCAN_PAGE";
      profile: UserApplicationProfile | null;
      fillEEO: boolean;
    }
  | { type: "FILL_FIELDS"; instructions: FillInstruction[] }
  | FormOpRequest
  | RemoteFormAvailable
  | RemoteFieldsUpdated
  | RemoteFlowProgress;

export interface PingResponse {
  ok: true;
  frameToken: string;
}

export interface ScanResponse {
  ok: boolean;
  error?: string;
  url: string;
  frameToken: string;
  fields: DetectedField[];
}

export interface FillInstruction {
  fieldId: string;
  value: string;
}

export interface FillOutcome {
  fieldId: string;
  ok: boolean;
  reason?: string;
}

export interface FillResponse {
  ok: boolean;
  error?: string;
  outcomes: FillOutcome[];
}

/** Fired by the content script when MutationObserver sees the form change. */
export interface FieldsUpdatedEvent {
  type: "FIELDS_UPDATED";
  url: string;
  fieldCount: number;
}

// ---------------------------------------------------------------------------
// Cross-frame form support (form lives in a child iframe; panel in top frame)
// ---------------------------------------------------------------------------

/** Every OverlayCallbacks method name, the generic form-op surface. */
export type FormOpName =
  | "onAutofill"
  | "onRescan"
  | "onListResumes"
  | "onUploadResume"
  | "onTailorResume"
  | "onAttachTailored"
  | "onDownloadTailored"
  | "onGenerateCoverLetter"
  | "onInsertCoverLetter"
  | "onDownloadCoverLetter"
  | "onCopyCoverLetter"
  | "onProfileResolved"
  | "onFlowStop"
  | "onFlowAdvance"
  | "onFlowPause"
  | "onAnswerGaps"
  | "onHarvestGapOptions";

/** One overlay operation, marshaled for execution in the form-owning frame. */
export interface FormOpRequest {
  type: "FORM_OP";
  op: FormOpName;
  args: unknown[];
}

/** Result of a FORM_OP, wrapping the callback's return value. */
export interface FormOpResult {
  ok: boolean;
  value?: unknown;
  error?: string;
}

/** A child frame telling the top frame it owns a real form. */
export interface RemoteFormAvailable {
  type: "REMOTE_FORM_AVAILABLE";
  frameId: number;
  recognized: number;
  fields: DetectedField[];
}

/** A child host pushing fresh fields (rescan / profile / mutation) to the top. */
export interface RemoteFieldsUpdated {
  type: "REMOTE_FIELDS_UPDATED";
  fields: DetectedField[];
  /** Field ids the child's terminal re-scan found no longer holding what was
   *  written. An array rather than a Set. This crosses a structured-clone
   *  boundary. Omitted on an ordinary rescan, which carries no new verdict. */
  reverted?: string[];
}

/** Child → background: "I own a form." Background forwards as REMOTE_FORM_AVAILABLE. */
export interface FormHostAnnounce {
  type: "FORM_HOST_ANNOUNCE";
  recognized: number;
  fields: DetectedField[];
}

/** Top → background → host frame: run this overlay op in the owning frame. */
export interface RelayFormOp {
  type: "RELAY_FORM_OP";
  frameId: number;
  op: FormOpName;
  args: unknown[];
}

/** Host → background → top frame (frameId 0): deliver a push payload. */
export interface RelayToTop {
  type: "RELAY_TO_TOP";
  payload: RemoteFormAvailable | RemoteFieldsUpdated | RemoteFlowProgress;
}

/** Isolated-world client → background: inject the MAIN-world driver bundle
 *  into the requesting frame (content scripts can't call chrome.scripting). */
export interface InstallMainWorldDriverRequest {
  type: "INSTALL_MAIN_WORLD_DRIVER";
}

// ---------------------------------------------------------------------------
// Popup <-> background messages
// ---------------------------------------------------------------------------

export type BackgroundRequest =
  | { type: "GET_STATUS" }
  | { type: "CONNECT" }
  | { type: "LOGOUT" }
  | { type: "GET_PROFILE"; forceRefresh?: boolean }
  | { type: "UPDATE_PROFILE"; update: Partial<UserApplicationProfile> }
  | { type: "GET_RESUMES" }
  | { type: "GET_SYNC"; forceRefresh?: boolean }
  | { type: "DOWNLOAD_RESUME"; resumeId: number }
  | { type: "OPEN_DASHBOARD" }
  | { type: "AI_FILL"; fields: AiFillField[]; jobContext: JobContext; profile?: ApplicantProfile }
  | {
      type: "TAILOR_RESUME";
      resumeId: number | null;
      jobContext: JobContext;
      sections?: string[];
      addKeywords?: string[] | null;
    }
  | { type: "RENDER_RESUME"; document: ResumeDoc; filename?: string }
  | {
      type: "GENERATE_COVER_LETTER";
      resumeId: number | null;
      jobContext: JobContext;
      tone?: string | null;
      baseText?: string | null;
    }
  | { type: "RENDER_COVER_LETTER"; text: string; filename?: string }
  | { type: "GET_ACCESS_TOKEN" }
  | { type: "FLOW_STATE_GET" }
  | { type: "FLOW_STATE_SET"; state: FlowState | null }
  | { type: "TOP_JOB_PLACE_GET" }
  | { type: "RECORD_APPLICATION"; application: ApplicationLog }
  | { type: "RECORD_TELEMETRY"; telemetry: AutofillTelemetry }
  | { type: "GET_OVERRIDES" }
  | { type: "DIAGNOSTIC_MODE" }
  | FormHostAnnounce
  | RelayFormOp
  | RelayToTop
  | InstallMainWorldDriverRequest;

/**
 * A job application the user submitted on an ATS page, recorded to their Tailrd
 * account (Applications page). Sent when the user clicks the real submit button
 * after an autofill flow, the extension never submits, it only observes.
 */
export interface ApplicationLog {
  company: string;
  role: string;
  url: string;
  /** Matched site adapter id (greenhouse, workday…) when known. */
  atsType?: string;
  /** "original" | "uploaded", which résumé the user attached this flow. */
  resumeVersion?: string;
  /** Internal Tailrd job id when the apply was launched from the dashboard. */
  jobId?: number | null;
}

export interface RecordApplicationResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  /** True when a new record was created; false when an existing one was refreshed. */
  created?: boolean;
}

/**
 * A server-authored classification override the extension applies as a hot-fix
 * (GET /autofill/overrides). Matched by host + a normalized-label regex; forces
 * a field category onto the generic adapter+AI pipeline. `valueSynonyms` is
 * reserved for future option-matching hints.
 */
export interface AutofillOverrideRule {
  host: string;
  labelPattern: string;
  category: string;
  valueSynonyms: Record<string, string>;
}

export interface OverridesResponse {
  ok: boolean;
  rules: AutofillOverrideRule[];
}

/**
 * One autofill pass summary, reported to the backend so we can see which sites
 * and fields the filler struggles with (the signal for authoring server-side
 * override rules). Field labels + outcomes only, never the user's values.
 */
/**
 * What happened to one field, whether or not it worked.
 *
 * Recording only FAILURES made the one bug class we keep hitting, reported
 * filled, actually wasn't, unobservable by construction. A field that the
 * pipeline answered wrongly but wrote successfully produced a `filled` count
 * and nothing else, so the record of the page said everything went well.
 *
 * Labels, categories, provenance and booleans only. Never the answer: the
 * privacy posture is that a user's answers do not leave the device except to
 * be typed into the employer's own form.
 */
export interface FieldOutcomeRecord {
  label: string;
  category: string;
  /** Which layer produced the value: "profile" | "backend" | "device" | "user". */
  tier: string;
  /** Which backend pass, when the value came from one: "derived" | "rule" |
   *  "ai"; "" for anything resolved without the backend. */
  pass: string;
  /** Did we intend to write anything here? */
  expectedValuePresent: boolean;
  /** Did the control hold a value when the page was re-scanned afterwards? */
  observedValuePresent: boolean;
  /**
   * "filled", written, and still holding a value at the terminal re-scan
   * "failed", the write itself did not land
   * "reverted", the write landed, and the value was gone (or changed) by the
   *               re-scan. This is the case per-write verification cannot see:
   *               a framework can reset a control on blur, on validation, or on
   *               a re-render some LATER field triggered.
   * "dropped", the backend gate refused the candidate value
   * "skipped", nothing was proposed for this field
   */
  outcome: "filled" | "failed" | "reverted" | "dropped" | "skipped";
  /** Failure/drop reason when there is one. Never contains an answer. */
  reason?: string;
}

/**
 * The full diagnostic record of one field, captured only for accounts that
 * turned diagnostic mode ON (see UserSettings.diagnosticCapture).
 *
 * This is a deliberately different object from FieldOutcomeRecord, and the
 * split is the privacy boundary: FieldOutcomeRecord is labels and booleans and
 * is sent for everybody; this carries the answer and the employer's markup and
 * is sent for nobody unless they asked for it.
 *
 * Every property here exists because its absence blocked a real diagnosis on
 * 2026-08-12/13: `controlType` (Greenhouse has no <select>s, which cost a live
 * page fetch to discover), `options` (the Degree list, which had to be guessed
 * at), `proposedValue` (the certification field's wrong answer was only caught
 * because its CATEGORY looked odd), `helpText` (the documented "trap"),
 * `durationMs` (the "takes exceptionally long" report had no data at all), and
 * `dom` (the only thing that lets an agent rebuild the form as a fixture).
 */
export interface FieldCaptureRecord {
  fieldId: string;
  /** Full label, NOT truncated to 200 like the outcome record's. */
  label: string;
  category: string;
  confidence: number;
  controlType: string;
  inputType: string;
  helpText: string;
  required: boolean;
  groupIndex: number | null;
  /** Options read from the live widget at capture time, not at scan time. */
  options: string[];
  /** What we tried to write. `redacted` says a secret was swapped for a marker. */
  proposedValue: string;
  /** What the control actually held at the terminal re-scan. */
  observedValue: string;
  redacted: boolean;
  tier: string;
  pass: string;
  outcome: string;
  reason: string;
  /** Sanitised markup around the control; drops into test/fixtures verbatim. */
  dom: string;
  selector: string;
}

/** Wall-clock cost of each phase, so "it was slow" becomes a query. */
export interface FillDurations {
  scanMs: number;
  localMs: number;
  backendMs: number;
  reaskMs: number;
  totalMs: number;
}

export interface AutofillTelemetry {
  host: string;
  atsType: string;
  url: string;
  totalFields: number;
  filled: number;
  failed: number;
  skipped: number;
  failedFields: { label: string; category: string; reason: string }[];
  /** Every attempted field, successes included. See FieldOutcomeRecord. */
  fieldOutcomes?: FieldOutcomeRecord[];
  /** Fields whose observed page state disagreed with what was written. */
  reverted?: number;
  /** Which build produced this report, so a stale local build is visible. */
  extensionVersion?: string;
  durations?: FillDurations;
  /** Diagnostic mode only; empty/absent for everyone else. */
  fieldCaptures?: FieldCaptureRecord[];
}

/**
 * A pending "apply intent" pushed from the Tailrd web app (externally_connectable
 * → onMessageExternal): the user clicked Apply on this job in the dashboard.
 * When they later submit on the ATS page, the submit tracker matches the page to
 * a recent intent by host and links the recorded application to the real job.
 */
export interface ApplyIntent {
  jobId: number;
  url: string;
  company?: string;
  title?: string;
  /** Epoch ms when the intent was recorded (for TTL). */
  ts: number;
}

export interface StatusResponse {
  ok: boolean;
  /** mock = sample data, connected = signed in, sessionExpired = was connected but refresh failed, signedOut = needs to connect */
  mode: "mock" | "connected" | "sessionExpired" | "signedOut";
  email?: string;
  /** Account name from /auth/me: used for the avatar before the profile loads. */
  firstName?: string;
  lastName?: string;
  apiBaseUrl: string;
  /** Subscription + usage from the cached snapshot (when connected). */
  subscription?: Subscription;
  usage?: Usage;
}

/** Full sync snapshot for the popup, with provenance. */
export interface SyncResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  snapshot?: ExtensionSyncSnapshot;
  source?: ProfileSource;
}

export interface ProfileResponse {
  ok: boolean;
  error?: string;
  /** True when the user must sign in before a real profile can be fetched. */
  needsLogin?: boolean;
  profile?: UserApplicationProfile;
  source?: ProfileSource;
}

export interface LoginResponse {
  ok: boolean;
  error?: string;
}

export interface ResumesResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  resumes: ResumeSummary[];
}

/** Resume file bytes (base64) for content-script injection into ATS forms. */
export interface ResumeFileResponse {
  ok: boolean;
  error?: string;
  needsLogin?: boolean;
  dataBase64?: string;
  name: string;
  contentType: string;
}

export interface SimpleResponse {
  ok: boolean;
  error?: string;
}

/** Answer to DIAGNOSTIC_MODE: does this account capture answers + form markup? */
export interface DiagnosticModeResponse {
  ok: boolean;
  enabled: boolean;
}

