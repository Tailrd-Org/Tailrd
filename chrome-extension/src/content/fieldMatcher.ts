/**
 * Field classification engine.
 *
 * Scores every text signal of a control (label, aria-label, placeholder,
 * name/id attributes, nearby text, autocomplete token, input type) against
 * per-category regex patterns and produces a category + confidence in 0..1.
 *
 * Design rules:
 *  - Signals are weighted by reliability: a real <label> beats an id attribute,
 *    which beats loose nearby text.
 *  - Negative patterns veto a signal for a category ("Email address" must not
 *    classify as location just because it contains "address").
 *  - Corroboration across multiple signals adds a small bonus.
 *  - Anything under MIN_CATEGORY_CONFIDENCE is reported as "unknown" rather
 *    than guessed, low-confidence guesses are surfaced for review, not filled.
 */
import { MIN_CATEGORY_CONFIDENCE } from "../shared/constants";
import type { ControlType, FieldCategory, ResolveControl, UserApplicationProfile } from "../shared/types";
import type { FieldSignals } from "./domUtils";
import { normalize } from "./optionMatch";
import { askedOfStatement } from "./statementText";
import { degreeRank, isHigh, profileFacts } from "./profileFacts";
import { countryFromName, regionFromText } from "./geo";

// Text normalization ("candidate-firstName" → "candidate first name") lives in
// optionMatch.ts, which the MAIN-world driver shares; re-exported for importers.
export { normalize };

// ---------------------------------------------------------------------------
// Pattern table
// ---------------------------------------------------------------------------

interface PatternSpec {
  re: RegExp;
  /** Pattern strength 0..1, lower for looser, more ambiguous patterns. */
  weight?: number;
}

interface CategorySpec {
  category: FieldCategory;
  patterns: PatternSpec[];
  /** If this matches a signal, that signal cannot vote for the category. */
  negative?: RegExp;
  sensitive?: boolean;
}

/**
 * A label that OPENS with an interrogative auxiliary is a yes/no question about
 * a datum, not a field asking you to type it. "May we contact your current
 * employer?" is answered "Yes", never "Acme Corp", but it out-scores every
 * generic pattern because it contains the exact phrase "current employer".
 *
 * Anchored to the start on purpose: "What is your current employer?" is also a
 * question, and that one DOES want the company name. The auxiliary is the only
 * thing that separates them.
 */
const YES_NO_QUESTION =
  /^(may|can|could|do|does|did|have|has|had|are|is|was|were|will|would|should|shall)\s+(we|i|you|your|they|it|he|she|there)\b/;

/**
 * Word count above which a label reads as prose (a statement or a multi-clause
 * question) rather than a field name. On prose, only exact patterns (weight 1)
 * may classify, see classifyField.
 */
const PROSE_WORDS = 25;

/** A link to ONE piece of work, not the profile: "Please share a link to a
 *  project you built with an LLM API (GitHub, demo, or write-up)" got the
 *  GitHub profile (United Placement Group on Workable, 2026-10-05). */
const ONE_PIECE_OF_WORK =
  /\b(link|url) to (a|one|your|the) (specific )?(project|demo|write ?up|sample|repo(sitory)?|example|app|application you)\b|\b(project|app|demo|tool|feature)s? (that )?you (have )?(built|made|created|developed|shipped|designed)\b/;

/** Someone else's name: "what is the employee's full name?" (Renaissance,
 *  question bank 2026-10-05) got the applicant's own. Labels are normalized
 *  ("employee's" reads "employee s"). */
const OTHER_PERSONS_NAME =
  /\b(employee|employees|referr\w*|referee|manager|supervisor|reference|recruiter|spouse|guardian|relative)( s)? (full |legal |first |last )?name\b/;

// Order matters only for tie-breaking: more specific categories come first.
const CATEGORY_SPECS: CategorySpec[] = [
  {
    // Today's date beside a signature. Lever names its disability-form date
    // eeo[disabilitySignatureDate] under a bare "Date" label (live 2026-10-03).
    // Listed first: "disability" in that name must not win the tie.
    category: "signatureDate",
    patterns: [{ re: /\b(signature date|date of signature|date signed|todays? date|today s date)\b/ }],
  },
  // --- EEO / demographics (sensitive, detected, never filled by default) ---
  {
    category: "eeoGenderIdentity",
    sensitive: true,
    patterns: [{ re: /\bgender identity\b/ }],
  },
  {
    category: "eeoGender",
    sensitive: true,
    patterns: [{ re: /\bgender\b/ }, { re: /\bsex\b/, weight: 0.85 }],
    // "Please share your gender pronouns" contains "gender", and eeoPronouns
    // scores identically on it, so the tie went to whichever spec is listed
    // first, this one. Lyft's pronouns list then got the gender answer:
    // 'No option matches "Male" (saw: She / Her | He / Him | …)'. Pronouns are
    // a different question with a different answer, so veto rather than reorder,
    // that way the fix holds wherever either spec moves to.
    negative: /\bsexual orientation\b|\bgender identity\b|\bpronouns?\b/,
  },
  {
    category: "eeoRace",
    sensitive: true,
    patterns: [{ re: /\brace\b/ }, { re: /\bethnicit(y|ies)\b/ }, { re: /\bethnic\b/ }],
  },
  {
    category: "eeoHispanic",
    sensitive: true,
    patterns: [{ re: /\bhispanic\b/ }, { re: /\blatino\b|\blatinx\b/ }],
  },
  {
    category: "eeoVeteran",
    sensitive: true,
    patterns: [{ re: /\bveteran\b/ }, { re: /\bmilitary (status|service)\b/ }],
  },
  {
    category: "eeoDisability",
    sensitive: true,
    patterns: [{ re: /\bdisabilit(y|ies)\b/ }, { re: /\bdisabled\b/ }, { re: /\bhandicap\b/ }],
    // Needing an accommodation is no disability status: "Do you require
    // workplace accommodations due to a physical disability…?" took Yes from
    // the status (Wattpad on Lever, question bank 2026-10-08).
    negative: /\b(do|will|would) you (need|require|request)\b[^?]{0,60}\baccommodat/,
  },
  {
    category: "eeoSexualOrientation",
    sensitive: true,
    patterns: [{ re: /\bsexual orientation\b/ }, { re: /\borientation\b/, weight: 0.8 }],
  },
  {
    // Ordered BEFORE eeoOther, which used to own this pattern: both score the
    // same on a "Pronouns" label, and ties go to the first spec listed.
    category: "eeoPronouns",
    sensitive: true,
    patterns: [{ re: /\bpronouns?\b/ }],
  },
  {
    category: "eeoOther",
    sensitive: true,
    patterns: [
      // "2SLGBTQI+" normalizes to "2 slgbtqi" (camelCase split): Coveo, live 2026-10-05.
      { re: /(?:\b2 ?[se] ?|\b)lgbtq?\w*\b/ },
      { re: /\btransgender\b/ },
      { re: /\bdemographic\b/ },
      { re: /\bequal employment\b|\beeoc?\b/ },
      { re: /\bdiversity\b/, weight: 0.8 },
    ],
  },

  // --- Identity ---
  {
    category: "firstName",
    negative: OTHER_PERSONS_NAME,
    patterns: [
      { re: /\b(first|given) name\b/ },
      { re: /\bfirstname\b|\bfname\b|\bforename\b/ },
      { re: /\bpreferred name\b/, weight: 0.75 },
    ],
  },
  {
    category: "lastName",
    // "Preferred first and last name" is BOTH (Voldex on Ashby, live
    // 2026-10-03: it got the last name alone).
    // "…your Full Legal First, Middle Initial, and Last Name" too (Block's
    // signature box got the last name alone, question bank 2026-10-05).
    negative: new RegExp(`\\b(first|given)\\b.{0,40}\\b(and|&) (last|family) name\\b|${OTHER_PERSONS_NAME.source}`),
    patterns: [
      { re: /\b(last|family) name\b/ },
      { re: /\blastname\b|\blname\b|\bsurname\b/ },
    ],
  },
  {
    category: "fullName",
    patterns: [
      { re: /\bfull name\b/ },
      { re: /\b(first|given)\b.{0,40}\b(and|&) (last|family) name\b/ },
      { re: /\bsign\w* by typing\b|\btyp(e|ing) your (full |legal |complete )*name\b/ },
      { re: /\blegal name\b/ },
      { re: /^(your )?name$/ },
      { re: /\bcomplete name\b/ },
      // Text signature ("type your name to sign / certify"). A drawing-pad
      // signature is a canvas (never a text input), so it won't reach here.
      { re: /\bsignature\b/, weight: 0.85 },
      { re: /\btype (your |in )?(full |legal )?name\b/, weight: 0.9 },
      { re: /\bsign(ature)? (here|below)\b/, weight: 0.85 },
      { re: /\bplease sign\b/, weight: 0.85 },
    ],
    // "first and last name" names the whole name, so its "first" / "last" do not
    // veto; nor do they in a signature typed out ("Please sign by typing your
    // Full Legal First, Middle Initial, and Last Name", Block, question bank
    // 2026-10-05: it got the last name alone).
    negative:
      /^(?!.*\b(sign\w* by typing|typ(?:e|ing) your (?:full |legal |complete )*(?:first|name))\b).*(?:\b(first|given)\b(?! (and|&) (last|family) name)|(?<!\b(first|given) (and|&) )\b(last|family)\b|\bmiddle\b|\buser ?name\b|\bcompany\b|\bemployer\b|\bschool\b|\bfile\b|\bcontact name\b|\bsign in\b|\bsign up\b|\b(employee|employees|referr\w*|referee|manager|supervisor|reference|recruiter|spouse|guardian|relative)( s)? (full |legal |first |last )?name\b)/,
  },
  {
    category: "email",
    patterns: [
      { re: /\be ?mail\b/ },
      { re: /\bcourriel\b/ }, // FR
      // FR "adresse électronique" (normalize() folds the accent: "adresse
      // electronique"; it once stripped it to a space, hence the optional e).
      { re: /adresse (e|é)?lectronique/ },
    ],
  },
  {
    category: "phone",
    patterns: [
      { re: /\bphone\b/ },
      { re: /\bmobile\b|\bcell\b|\btelephone\b/ },
    ],
    negative: /\bphone type\b|\bext(ension)?\b|\bcountry code\b|\bdevice type\b|\b(dial(ing)?|calling) code\b/,
  },
  {
    // The phone number's dialing-code picker ("Country code", "+1"). It used
    // to fall to `location` on the word "country" and receive the applicant's
    // city (SmartRecruiters, live 2026-10-03). questionResolver answers it with
    // the country (or "+1") rendered into the picker's own options.
    category: "phoneCountryCode",
    patterns: [
      { re: /\b(country|dial(ing)?|calling|phone|telephone|mobile) code\b/ },
      { re: /\bcountry calling\b/ },
    ],
    negative: /\b(postal|zip|area|promo|discount|referral|verification|security|access|invite|invitation) code\b/,
  },

  // --- Structured address, more specific than the generic `location`, so these
  //     are ordered BEFORE it: an explicit street/city/state/postal/country field
  //     wins the tie, while a bare "Address"/"Location" field still falls to
  //     `location`. FR keywords included; normalize() folds accents ("région"
  //     → "region"; \bgion\b stays for text stripped the old way), and
  //     "province"/"ville"/"pays"/"adresse"/"code postal" are unaccented.
  {
    category: "addressStreet",
    patterns: [
      { re: /\bstreet\b/ },
      { re: /\baddress ?line ?\d?\b/ },
      // "Address 2" (Jobvite, question bank 2026-10-17) fell to `location`
      // below and took "Seattle, WA": it is the street's second line.
      { re: /\baddress ?[12]\b/ },
      { re: /\bcivic\b/ },
      { re: /\badresse\b/ }, // FR
    ],
    // "address" alone is ambiguous with "email address", an email field must
    // never classify as a street address. FR: "adresse courriel" / "adresse
    // électronique" (normalize() folds the accent → "electronique").
    negative: /\be ?mail\b|\bip address\b|\bcourriel\b|\be?lectronique\b/,
  },
  {
    category: "addressCity",
    patterns: [
      { re: /\bcity\b/ },
      { re: /\btown\b/ },
      { re: /\bville\b/ }, // FR
    ],
  },
  {
    category: "addressState",
    patterns: [
      { re: /\bstate\b/, weight: 0.9 },
      { re: /\bprovince\b/ },
      { re: /\bregion\b|\bgion\b/, weight: 0.85 }, // \bgion\b catches FR "région"
    ],
    // "Please state the country you were born in" (GDMSI on SmartRecruiters,
    // bank): the verb.
    negative: /\bstatement\b|\bplease state\b|\bstate (the|your|whether|if|why|how|what|which|any|below)\b/,
  },
  {
    category: "postalCode",
    patterns: [
      { re: /\bpostal code\b/ },
      { re: /\bpost ?code\b/ }, // "Postcode" (UK; Pinpoint, live 2026-10-03)
      { re: /\bzip( ?code)?\b/ },
      { re: /\bcode postal\b/ }, // FR
      { re: /\bpostal\b/, weight: 0.9 },
    ],
  },
  {
    category: "country",
    patterns: [
      { re: /\bcountry\b/, weight: 0.9 },
      { re: /\bpays\b/ }, // FR
    ],
    // A phone "country code" selector is not the mailing country, and no
    // profile holds the country someone was born in (GDMSI on SmartRecruiters).
    negative: /\bcountry code\b|\bborn\b|\bbirth/,
  },

  {
    category: "location",
    patterns: [
      { re: /\bcity\b|\btown\b/ },
      { re: /\blocation\b/ },
      { re: /\baddress\b/, weight: 0.85 },
      { re: /\bstate\b|\bprovince\b|\bregion\b/, weight: 0.7 },
      { re: /\bcountry\b/, weight: 0.75 },
      { re: /\bpostal\b|\bzip\b/, weight: 0.7 },
      // "Where are you currently located?" (TensorWave, ConductorAI on Ashby,
      // 2026-10-08): an adverb between never stopped it being the question.
      { re: /\bwhere (are you|do you) ((currently|presently|now) )?(located|based|live|living|reside|residing)\b/ },
    ],
    // Never the place someone was born (GDMSI on SmartRecruiters, bank).
    negative: /\be ?mail\b|\bip address\b|\bweb ?site\b|\burl\b|\blinked ?in\b|\bgit ?hub\b|\bcountry code\b|\b(dial(ing)?|calling) code\b|\bborn\b|\bbirth/,
  },

  // --- Links ---
  {
    category: "linkedin",
    patterns: [{ re: /\blinked ?in\b/ }],
    negative: ONE_PIECE_OF_WORK,
  },
  {
    category: "github",
    patterns: [{ re: /\bgit ?hub\b/ }],
    negative: ONE_PIECE_OF_WORK,
  },
  {
    category: "portfolio",
    patterns: [
      { re: /\bportfolio\b/ },
      { re: /\bpersonal (web ?site|site|url|page)\b/ },
      { re: /\bweb ?site\b/, weight: 0.8 },
      { re: /\bother (url|link|web ?site)\b/, weight: 0.7 },
      { re: /\bblog\b/, weight: 0.6 },
    ],
    negative: new RegExp(/\blinked ?in\b|\bgit ?hub\b|\bcompany (web ?site|url)\b|\btwitter\b|\bfacebook\b|/.source + ONE_PIECE_OF_WORK.source),
  },

  // --- Documents ---
  {
    category: "resumeUpload",
    patterns: [
      { re: /\bresume\b|\bresum\b/ }, // "résumé" loses accents in normalize()
      { re: /\bcv\b|\bcurriculum vitae\b/ },
    ],
    negative: /\bcover letter\b/,
  },
  {
    category: "coverLetter",
    patterns: [
      { re: /\bcover letter\b|\bcoverletter\b/ },
      { re: /\bletter of (motivation|interest)\b|\bmotivation letter\b/ },
      { re: /\bwhy (do you want|are you interested|would you like) /, weight: 0.6 },
    ],
  },

  // --- Screening ---
  {
    category: "sponsorship",
    patterns: [
      { re: /\bsponsorship\b/ },
      { re: /\bsponsor\b/, weight: 0.9 },
      { re: /\b(visa|immigration) (status|support)\b/, weight: 0.8 },
    ],
  },
  {
    category: "workAuthorization",
    patterns: [
      { re: /\bwork authori[sz]ation\b/ },
      { re: /\bauthori[sz]ed to work\b/ },
      { re: /\blegally (authori[sz]ed|eligible|entitled)\b/ },
      { re: /\beligible to work\b|\bright to work\b/ },
      { re: /\bwork permit\b/, weight: 0.85 },
    ],
    negative: /\bsponsor/,
  },

  // --- Screening answers stated on the profile (2026-08-09 parity contract) ---
  // Each of these is answered from a fact the user typed once, so a matching
  // question never reaches the AI pass. Their guards matter more than their
  // patterns: a near-miss here writes a confident wrong answer into a real
  // application, whereas abstaining just routes the field to the grounded AI.
  {
    category: "willingToRelocate",
    patterns: [
      { re: /\bwilling(ness)? to relocate\b/ },
      { re: /\b(open|able|willing) to relocat(e|ion|ing)\b/ },
      { re: /\bwould you relocate\b/ },
      { re: /\brelocat(e|ion|ing)\b/, weight: 0.85 },
    ],
    // "Do you require relocation assistance?" is a benefits question, not a
    // willingness one, answering it "Yes" from a willingness answer is wrong.
    // Paying one's own way is the applicant's too: "…provide your own housing,
    // relocation, and transportation to the internship site?" took Yes from
    // the willingness (The Exploration Company on Ashby, live 2026-10-08).
    negative:
      /\brelocation (assistance|package|support|expenses?|benefits?|allowance|reimbursement)\b|\bown (housing|relocation|transportation|travel|moving|move)\b|\bat (your|their|my|one s) own (cost|expense)\b|\b(pay|paying|cover|covering|fund|funding) (for )?(your |the )?(own )?(relocation|move|moving|housing)\b/,
  },
  {
    category: "workPreference",
    patterns: [
      { re: /\bwork (preference|arrangement|setting|model|mode)\b/ },
      { re: /\bworking (preference|arrangement)\b/ },
      { re: /\bremote or on ?site\b|\bon ?site or remote\b/ },
      { re: /\bremote or hybrid\b|\bhybrid or remote\b/ },
      { re: /\b(remote|hybrid|on ?site) work preference\b/ },
    ],
    // A geographic "preferred work location" is the `location` question.
    negative: /\bwork location\b|\blocation preference\b/,
  },
  {
    category: "noticePeriod",
    patterns: [
      { re: /\bnotice period\b/ },
      { re: /\bperiod of notice\b/ },
      { re: /\bhow (much|long a|many weeks|many days) notice\b/ },
      { re: /\bnotice (required|do you|to give|you (must|need to) give)\b/ },
      { re: /\bcurrent notice\b/ },
    ],
  },
  {
    // Availability: "when can you start?". Distinct from experienceStartDate,
    // whose own negative already vetoes "earliest" / "available" / "when can
    // you", so the two can never both claim a signal.
    category: "startDate",
    patterns: [
      { re: /\bearliest (possible )?(start|starting|available|availability|joining) date\b/ },
      { re: /\bearliest (date you can start|availability|start)\b/ },
      { re: /\b(available|availability|preferred|desired|proposed|anticipated|expected|potential|possible) (start|starting|joining) date\b/ },
      { re: /\bwhen (can|could|would) you (be able to )?(start|begin|join|commence)\b/ },
      { re: /\bdate available (to start|for work|to begin)\b/ },
      { re: /\bavailability to start\b|\bstart availability\b/ },
      { re: /\bdate you can start\b|\bhow soon can you start\b/ },
      // "If chosen for the role, how soon are you able to start?" (Kaizen on
      // JazzHR, live 2026-10-03, left for the AI).
      { re: /\bhow soon (are|would|will) you (be )?(able|available) to (start|begin|join)\b/ },
      // "On what date would you be available to work" (Barnes & Thornburg,
      // Ashby bank 2026-10-08).
      { re: /\bwhat date (would|could|can|will) you (be )?(available|able) to (start|begin|work|join|commence)\b/ },
    ],
    // Never a birth date, and never an employment-history row's own start date.
    negative: /\bbirth\b|\bemployment start\b|\bprevious\b|\bmost recent (job|role|position|employer)\b/,
  },
  {
    // The headline number only. "Years of experience with Python" is a SKILL
    // question whose answer is not on the profile, see the negative below.
    category: "yearsOfExperience",
    patterns: [
      // "years experience", "years of experience", "years of relevant work
      // experience", any run of the usual qualifiers, but nothing else.
      { re: /\byears?(?: of)? (?:(?:relevant|professional|work|total|overall|paid|full time|industry)\s+)*experience\b/ },
      { re: /\btotal (years?|work|professional) experience\b/ },
      { re: /\bexperience \(years\)\b|\bexperience in years\b/ },
    ],
    // Anything that qualifies WHAT the experience is in ("…experience with
    // React", "…experience do you have in project management") is a different
    // question with a different answer; abstain and let the grounded AI answer.
    negative: /\bexperience\b[\s\S]*\b(with|in|using|related to)\b|\bexperience level\b/,
  },
  {
    category: "securityClearance",
    patterns: [
      { re: /\bsecurity clearance\b/ },
      { re: /\bclearance (level|status|held)\b/ },
      { re: /\b(active|current|existing|valid) clearance\b/ },
      { re: /\bclearance\b/, weight: 0.85 },
    ],
    // Customs / credit / medical "clearance" is not a security clearance.
    negative: /\bcustoms\b|\bcredit\b|\bmedical\b|\bclearance sale\b/,
  },
  {
    // normalize() turns "Driver's" into "driver s", so both spellings and both
    // apostrophe forms have to be reachable from one pattern.
    category: "driversLicense",
    patterns: [
      { re: /\bdrivers? (s )?licen[sc]e\b/ },
      { re: /\bdriving licen[sc]e\b/ },
      { re: /\bpermis de conduire\b/ }, // FR
    ],
    // A license NUMBER is a data-entry field, not the yes/no screening question.
    negative: /\blicen[sc]e (number|no|#)\b/,
  },
  {
    category: "languages",
    patterns: [
      { re: /\blanguages? (spoken|you speak)\b/ },
      { re: /\bspoken languages?\b/ },
      { re: /\blanguage (proficiency|fluency|skills?|ability|abilities)\b/ },
      { re: /\blanguages?\b/, weight: 0.9 },
    ],
    // Programming languages are the `skills` answer, and "preferred language"
    // is almost always the site's own UI locale.
    negative: /\bprogramming\b|\bcoding\b|\bcomputer\b|\bmarkup\b|\btechnical\b|\bpreferred language\b|\blanguage of (correspondence|communication|instruction)\b/,
  },

  // --- Education ---
  {
    category: "school",
    patterns: [
      { re: /\bschool\b|\buniversity\b|\bcollege\b/ },
      { re: /\binstitution\b|\balma mater\b/ },
    ],
    negative: /\bhigh school diploma\b/,
  },
  {
    category: "degree",
    patterns: [
      { re: /\bdegree\b/ },
      { re: /\b(highest )?(level of )?education( level)?\b/, weight: 0.75 },
      { re: /\bqualification\b/, weight: 0.7 },
    ],
    // Workday shows Degree and Field of Study side by side; a "field of study"
    // label must never resolve here or the subject dropdown gets "BSc …".
    negative: /\bfield of study\b|\bmajor\b|\bdiscipline\b|\bconcentration\b/,
  },
  {
    category: "fieldOfStudy",
    patterns: [
      { re: /\bfield of study\b|\bmajor\b|\b(area|program|programme) of study\b/ },
      { re: /\bdiscipline\b|\bconcentration\b|\bcourse of study\b/, weight: 0.85 },
    ],
  },
  {
    category: "graduationYear",
    patterns: [
      { re: /\bgraduation (year|date)\b|\bgrad year\b/ },
      { re: /\b(year of|expected) graduation\b/ },
      { re: /\bcompletion (year|date)\b/, weight: 0.8 },
    ],
  },
  {
    category: "education",
    patterns: [{ re: /\beducation\b/, weight: 0.7 }],
    negative: /\blevel\b|\bhighest\b/,
  },

  // --- Work history ---
  {
    category: "currentCompany",
    patterns: [
      { re: /\bcurrent (company|employer)\b/ },
      { re: /\b(most recent|recent) (company|employer)\b/ },
      { re: /\bemployer\b/, weight: 0.85 },
      { re: /\bcompany( name)?\b/, weight: 0.7 },
      { re: /\borganization\b/, weight: 0.6 },
    ],
    negative: new RegExp(
      // "May we contact your current employer?" is a Yes/No screening question;
      // it was classified here and would have answered with a company name.
      YES_NO_QUESTION.source + "|" +
      /\bprevious\b|\bformer\b|\bsponsor\b|\bschool\b|\breferr(al|er)\b/.source
    ),
  },
  {
    category: "currentTitle",
    patterns: [
      { re: /\b(current |present )?(job )?title\b/, weight: 0.9 },
      { re: /\bcurrent (role|position)\b/ },
      // The whole label only: Dayforce's "Current Job" checkbox in each work
      // row (live 2026-10-05) went to the AI as "experience".
      { re: /^current job$/ },
      { re: /\bposition\b/, weight: 0.6 },
    ],
    // Another person's title: "Supervisor Name & Title:" got the applicant's
    // own (Saalex on Workable, 2026-10-05).
    negative: /\bsalutation\b|\bmr\b|\bmrs\b|\bdegree\b|\bapply(ing)?\b|\bapplied\b|\bdesired\b|\binterested\b|\bsong\b|\bsupervisor\b|\breferences?\b|\breferr\w*\b/,
  },
  {
    category: "experienceStartDate",
    patterns: [
      { re: /\bstart date\b/ },
      { re: /\bstart (month|year)\b/ },
      { re: /\bdate started\b|\bstarted\b/, weight: 0.75 },
      { re: /\bemployment start\b/ },
    ],
    negative: /\bavailable\b|\bearliest\b|\bnotice\b|\bwhen can you\b/,
  },
  {
    category: "experienceEndDate",
    patterns: [
      { re: /\bend date\b/ },
      { re: /\bend (month|year)\b/ },
      { re: /\bdate ended\b|\bended\b/, weight: 0.75 },
      { re: /\bemployment end\b/ },
    ],
  },
  {
    category: "experienceCurrent",
    patterns: [
      { re: /\bcurrently work(ing)? here\b/ },
      { re: /\bi currently work\b/ },
      { re: /\bpresently (work|employ)/ },
      { re: /\bmy current (job|role|position|employer)\b/ },
    ],
  },
  {
    category: "experienceDescription",
    patterns: [
      { re: /\b(job|role|position) description\b/ },
      { re: /\bresponsibilit(y|ies)\b/ },
      { re: /\b(key )?(duties|accomplishments|achievements)\b/, weight: 0.8 },
      { re: /\bdescribe your (role|work|responsibilit)/ },
    ],
  },
  {
    category: "experience",
    patterns: [
      { re: /\byears? of (relevant |professional |work )?experience\b/, weight: 0.9 },
      { re: /\bwork experience\b|\bemployment history\b|\bwork history\b/ },
      { re: /\b(describe|tell us about) your (relevant )?experience\b/ },
      { re: /\bexperience\b/, weight: 0.5 },
    ],
    negative: /\bexperience level\b/,
  },
  {
    category: "salary",
    patterns: [
      { re: /\bsalary\b/ },
      { re: /\bcompensation\b/, weight: 0.9 },
      { re: /\b(desired|expected) (pay|salary|compensation|rate)\b/ },
      { re: /\bpay expectations?\b/ },
      { re: /\b(hourly|daily) rate\b/, weight: 0.8 },
    ],
    // The pay now is never given; "current salary requirements" are the
    // expectation (Trace3 on Greenhouse, live, round 8).
    negative: /\bcurrent salary\b(?! (requirements?|expectations?|range))/,
  },
  {
    category: "skills",
    patterns: [
      { re: /\bskills?\b/ },
      { re: /\bareas? of (expertise|specialization)\b/ },
      { re: /\b(technical|core|key) competenc(y|ies)\b/, weight: 0.9 },
      { re: /\btech(nologies)? (you|used)\b/, weight: 0.8 },
    ],
    // A rating of one skill is no list of skills: "How would you rate your
    // closing skills?" got "Python, C++, ROS" (United Placement Group on
    // Workable, 2026-10-05).
    negative: /\bcover letter\b|\blanguage\b|\brat(e|ing)\b|\bscale of\b|\bhow (good|strong|proficient|comfortable)\b/,
  },
];

/**
 * HTML autocomplete tokens are the most reliable signal there is, the site
 * itself declared the semantic meaning of the field.
 */
const AUTOCOMPLETE_MAP: Record<string, FieldCategory> = {
  "given-name": "firstName",
  "additional-name": "unknown", // middle name. We have no data for it
  "family-name": "lastName",
  name: "fullName",
  email: "email",
  tel: "phone",
  "tel-national": "phone",
  "address-level1": "addressState",
  "address-level2": "addressCity",
  "street-address": "addressStreet",
  "address-line1": "addressStreet",
  "postal-code": "postalCode",
  country: "country",
  "country-name": "country",
  organization: "currentCompany",
  "organization-title": "currentTitle",
};

/** Reliability of each signal source. */
const SOURCE_WEIGHTS: Array<{ key: keyof FieldSignals; weight: number }> = [
  { key: "label", weight: 0.95 },
  { key: "ariaLabel", weight: 0.92 },
  { key: "placeholder", weight: 0.82 },
  { key: "nameAttr", weight: 0.72 },
  { key: "testId", weight: 0.7 },
  { key: "idAttr", weight: 0.66 },
  { key: "nearby", weight: 0.6 },
];

export interface Classification {
  category: FieldCategory;
  confidence: number;
  sensitive: boolean;
}

export function classifyField(signals: FieldSignals): Classification {
  // Pre-normalize every text source once.
  const texts: Array<{ weight: number; text: string; prose: boolean }> = [];
  for (const { key, weight } of SOURCE_WEIGHTS) {
    const raw = signals[key];
    if (raw) {
      const full = normalize(raw);
      const words = full.split(" ").length;
      // A statement stays prose, though only its asked part is read.
      const text = key === "label" || key === "ariaLabel" ? normalize(askedOfStatement(raw, words)) : full;
      if (text) {
        texts.push({ weight, text, prose: words >= PROSE_WORDS });
      }
    }
  }

  let best: Classification = { category: "unknown", confidence: 0, sensitive: false };

  for (const spec of CATEGORY_SPECS) {
    let max = 0;
    let matchedSources = 0;
    for (const { weight, text, prose } of texts) {
      if (spec.negative?.test(text)) continue;
      let sourceBest = 0;
      for (const { re, weight: pw } of spec.patterns) {
        // On prose, a loose keyword is a coincidence, not a classification.
        // Lyft's 431-char certification statement says "Employer" three times
        // ("I authorize the Employer to make an investigation…"), which the
        // weight-0.85 /\bemployer\b/ pattern read as an employer FIELD, so the
        // applicant's company name was typed into Lyft's legal certification
        // box (autofill_reports #167, 2026-08-12). Exact patterns still fire:
        // the equally prose-y "…are you open to relocating?" carries the
        // weight-1 phrase "open to relocating" and stays willingToRelocate.
        if (prose && (pw ?? 1) < 1) continue;
        if (re.test(text)) sourceBest = Math.max(sourceBest, weight * (pw ?? 1));
      }
      if (sourceBest > 0) {
        matchedSources++;
        max = Math.max(max, sourceBest);
      }
    }

    // Native input type hints.
    if (signals.typeHint === "email" && spec.category === "email") max = Math.max(max, 0.9);
    if (signals.typeHint === "tel" && spec.category === "phone") max = Math.max(max, 0.9);
    if (signals.typeHint === "url" && spec.category === "portfolio") max = Math.max(max, 0.45);

    // The site's own autocomplete declaration is near-certain.
    const acCategory = AUTOCOMPLETE_MAP[signals.autocomplete];
    if (acCategory && acCategory === spec.category) max = Math.max(max, 0.98);

    if (max > 0) {
      // Small corroboration bonus when several independent signals agree.
      const score = Math.min(0.99, max + 0.04 * Math.max(0, matchedSources - 1));
      if (score > best.confidence) {
        best = { category: spec.category, confidence: score, sensitive: spec.sensitive ?? false };
      }
    }
  }

  if (best.confidence < MIN_CATEGORY_CONFIDENCE) {
    return { category: "unknown", confidence: best.confidence, sensitive: false };
  }
  return { ...best, confidence: Math.round(best.confidence * 100) / 100 };
}

// ---------------------------------------------------------------------------
// Profile value resolution
// ---------------------------------------------------------------------------

function hasYesNoOptions(options: string[] | undefined): boolean {
  if (!options || options.length === 0) return false;
  const norm = options.map((o) => normalize(o));
  return norm.some((o) => o.startsWith("yes")) && norm.some((o) => o.startsWith("no"));
}

/** A control answered with a Yes/No choice: explicit Yes/No options, or a
 *  custom dropdown whose options we can't read at scan time. */
function isYesNoChoice(control: ResolveControl): boolean {
  return hasYesNoOptions(control.options) || control.controlType === "combobox";
}

/** The kinds of school a "School Type" list offers (never a school's name). */
const SCHOOL_KIND =
  /^(high school|secondary school|community college|vocational( college| school)?|technical( college| school| institute)?|trade school|college|university|college ?\/ ?university|graduate school|specialized|specialty school|online|military|other)$/i;

/**
 * The option naming the KIND of school an education row is, or undefined when
 * the options are not a list of kinds (a list of schools, a free-text box).
 */
function schoolTypeOption(edu: { school?: string; degree?: string } | undefined, control: ResolveControl): string | null | undefined {
  if (!CHOICE_CONTROLS.includes(control.controlType) || !control.options?.length) return undefined;
  const real = control.options.filter((o) => o.trim() && !/^(-+|select\b|choose\b|unspecified$)/i.test(o.trim()));
  if (real.length < 2 || !real.every((o) => SCHOOL_KIND.test(o.trim()))) return undefined;
  if (!edu) return null;
  const name = edu.school ?? "";
  const pick = (re: RegExp): string | null => {
    const hits = real.filter((o) => re.test(o));
    return hits.length === 1 ? hits[0] : null;
  };
  if (/\b(high|secondary) school\b/i.test(name)) return pick(/high school|secondary/i);
  if (/\bcommunity college\b/i.test(name)) return pick(/community college/i);
  if ((degreeRank(edu.degree ?? "") ?? 0) >= 4 || /\b(universit|college|institute|polytechnic|ecole|école)/i.test(name)) {
    return pick(/universit/i) ?? pick(/^college$/i);
  }
  return null;
}

/** "Authorized to work in Canada" → Yes; "Not authorized" / "No" → No. */
function toYesNo(statement: string): string {
  return /^\s*(no\b|not\b|none\b)/i.test(statement) ? "No" : "Yes";
}

function formatExperience(profile: UserApplicationProfile): string | null {
  if (!profile.experience?.length) return null;
  return profile.experience
    .map((e) => {
      const dates = [e.startDate, e.endDate].filter(Boolean).join(" to ");
      const header = [e.title, e.company].filter(Boolean).join(", ");
      return [`${header}${dates ? ` (${dates})` : ""}`, e.description].filter(Boolean).join("\n");
    })
    .join("\n\n");
}

function formatEducation(profile: UserApplicationProfile): string | null {
  const ed = profile.education?.[0];
  if (!ed) return null;
  const parts = [ed.degree, ed.school].filter(Boolean).join(", ");
  return parts ? `${parts}${ed.graduationYear ? ` (${ed.graduationYear})` : ""}` : null;
}

/** Qualification words that PREFIX a degree string, with an optional joining
 *  "in"/"of". Ordered longest-first so "Bachelor of Science" is consumed whole
 *  before the bare "Bachelor" alternative can match.
 *
 *  The `\b` after the group is load-bearing, without it "Barts" reads as
 *  "B.A." + "rts". But `\b` cannot hold between a trailing "." and a space, so
 *  the abbreviation's OWN final period ("B.S. Electrical Engineering") is only
 *  reachable by the `\.?` that follows the boundary. */
const DEGREE_PREFIX_RE =
  /^\s*(bachelor(?:'?s)?(?:\s+of\s+(?:science|arts|engineering|commerce|business))?|master(?:'?s)?(?:\s+of\s+(?:science|arts|engineering|business administration))?|doctor(?:ate)?(?:\s+of\s+philosophy)?|ph\.?\s?d\.?|b\.?\s?(?:sc?|a|eng|comm)\.?|m\.?\s?(?:sc?|a|eng|ba)\.?|associate(?:'?s)?|diploma|certificate)\b\.?[\s,]*(?:degree\b[\s,]*)?(?:in|of)?\b[\s,]*/i;

/**
 * The subject of a degree string, Workday's "Field of Study" dropdown, which
 * is a SEPARATE control from "Degree" and rejects the degree's own text.
 *
 * "BSc Computer Science" / "Bachelor of Science in Computer Science" →
 * "Computer Science". A degree naming no subject ("Bachelor's Degree", "PhD")
 * returns null, which routes the field to the grounded AI pass rather than
 * writing something invented.
 *
 * ONLY the first comma segment is returned, so a double major ("BSc Computer
 * Science, Mathematics") proposes "Computer Science" and never "Computer
 * Science, Mathematics". That truncation is a safety property, not a cosmetic
 * one: comboboxEngine treats any widget under a `multiselectInputContainer` as
 * multi-select and SPLITS a comma-bearing value into one pick per item, but
 * that container also wraps Workday prompts that are single-value in fact,
 * where each pick REPLACES the last (see test/fixtures/workdayReal.ts). Handing
 * such a widget "Computer Science, Mathematics" commits "Mathematics" and
 * reports a successful fill: the wrong value, banked green, invisible to the
 * user. Keeping a comma out of this path makes that unreachable from here, and
 * on a single-value Field of Study the first subject is the honest answer
 * anyway.
 */
/** A German Diplom title's field ("Diplom-Informatiker", "Dipl.-Ing."). */
const DIPLOM_FIELDS: ReadonlyArray<[RegExp, string]> = [
  [/^wirtschaftsinformatik/, "Information Systems"],
  [/^informatik/, "Computer Science"],
  [/^ing(enieur)?$/, "Engineering"],
  [/^(kaufmann|kauffrau|kfm|betriebswirt)/, "Business Administration"],
  [/^volkswirt/, "Economics"],
  [/^physik/, "Physics"],
  [/^mathematik/, "Mathematics"],
  [/^chemik/, "Chemistry"],
  [/^biolog/, "Biology"],
  [/^psycholog/, "Psychology"],
];

export function deriveFieldOfStudy(degree: string): string | null {
  // A German Diplom names its field by the graduate's title: "Diplom-
  // Informatiker" is computer science (the SmartRecruiters bank, round 8: no
  // major at all from it).
  const diplom = /^\s*(?:dipl(?:om)?\.?)[\s-]+([a-zäöüß]+)/i.exec(degree || "");
  if (diplom) {
    const title = diplom[1].toLowerCase();
    const field = DIPLOM_FIELDS.find(([re]) => re.test(title));
    if (field) return field[1];
  }
  // "<qualification> in <subject>": the subject follows " in ", whatever the
  // qualification says. The prefix list below cannot name every "of X"
  // variant: "Bachelor of Applied Science in Mechatronics Engineering" left
  // "Applied Science in Mechatronics Engineering" (Workable, live 2026-10-03).
  const inForm = /^\s*(?:bachelor|master|doctor|associate|diploma|certificate|ph\.?\s?d|b\.?\s?[a-z]{1,4}\.?|m\.?\s?[a-z]{1,4}\.?)\b[^,]*?\bin\s+(.+)$/i.exec(degree || "");
  if (inForm && !/^(progress|view|process)\b/i.test(inForm[1].trim())) {
    return inForm[1].split(",")[0].trim() || null;
  }
  // French: "<qualification> en <subject>" ("Baccalauréat en psychologie", a
  // Montreal applicant, 2026-10-03), capitalized as a field on its own.
  const enForm = /^\s*(?:baccalaur[ée]at|ma[iî]trise|doctorat|certificat|dipl[ôo]me|licence|d\.?e\.?c\.?)\b[^,]*?\ben\s+(.+)$/i.exec(degree || "");
  if (enForm) {
    const subject = enForm[1].split(",")[0].trim();
    return subject ? subject.charAt(0).toUpperCase() + subject.slice(1) : null;
  }
  const rest = (degree || "").replace(DEGREE_PREFIX_RE, "").trim();
  // Compared BEFORE truncating: a bare "Computer Science, Mathematics" with no
  // degree prefix names no degree at all, and must still return null.
  if (!rest || rest.toLowerCase() === (degree || "").trim().toLowerCase()) return null;
  // "Bachelor's degree in progress" names a status, not a subject.
  if (/^(progress|view|process)\b/i.test(rest)) return null;
  return rest.split(",")[0].trim() || null;
}

/**
 * "Is this your current role?" answered from one experience row's end date:
 * "yes" only for the row that has none (or names the present). Abstains when
 * the row is unknown, so a standalone checkbox never gets a guessed answer.
 */
function currentRoleFlag(profile: UserApplicationProfile, gi: number | null): string | null {
  // An unnumbered row is filled with the CURRENT job (its Company and Title
  // resolve to it), so its "I currently work here" box is ticked when there is
  // one (Workable, live 2026-10-03: it went to the AI).
  if (gi === null) return isHigh(profileFacts(profile).employment.currentCompany) ? "yes" : null;
  const end = profile.experience?.[gi]?.endDate;
  if (end === undefined) return null;
  return !end.trim() || /\b(present|current|now|ongoing|to date)\b/i.test(end) ? "yes" : "no";
}

/**
 * Canonical degree LEVELS, longest/highest first so "Master of Business
 * Administration" is read as an MBA and not as a plain Master's, and a
 * transcript naming several degrees resolves to the highest one.
 *
 * Abbreviations require their periods (`M.A.`, `B.Sc`) or a distinctive spelling
 * (`msc`, `beng`). A bare two-letter alternative would match inside ordinary
 * words, and mislabelling somebody's degree is worse than abstaining.
 */
const DEGREE_LEVELS: Array<[RegExp, string]> = [
  // Quebec's university degrees name their field after "en": "Baccalauréat
  // en psychologie" is a bachelor's (Hear.com on Greenhouse, live, round 8,
  // left blank). A bare baccalauréat is France's school diploma: not matched.
  [/\bph\.?\s?d\b|\bdoctorate\b|\bdoctor of philosophy\b|\bd\.?phil\b|\bdoctorat en\b/i, "Doctorate"],
  [/\bm\.?b\.?a\b|\bmaster of business administration\b/i, "MBA"],
  [/\bmaster(?:'?s)?\b|\bm\.\s?(?:sc?|a|eng)\.?\b|\bmsc\b|\bmeng\b|\bma[iî]trise en\b/i, "Master"],
  [/\bbachelor(?:'?s)?\b|\bb\.\s?(?:sc?|a|eng|comm)\.?\b|\bbsc\b|\bbeng\b|\bundergraduate\b|\bbaccalaur[ée]at en\b/i, "Bachelor"],
  [/\bassociate(?:'?s)?\b/i, "Associate"],
  // Before "Diploma": a "High School Diploma" is a high school, and read as a
  // Diploma it matched none of Epic Games' degree options (live 2026-10-05).
  [/\bhigh school\b|\bsecondary school\b/i, "High School"],
  [/\bdiploma\b/i, "Diploma"],
  [/\bcertificate\b/i, "Certificate"],
];

/**
 * The level a degree title names, for a Degree control that offers levels
 * rather than titles: "Bachelor of Applied Science (Honours) in Software
 * Engineering" → "Bachelor", which matchOption then snaps onto Greenhouse's
 * "Bachelor's Degree".
 *
 * Returns null when the string names no recognizable level, so an unusual
 * qualification keeps its own text instead of being forced into a bucket.
 *
 * Why this exists: on the real Lyft form (autofill_reports #168, 2026-08-13)
 * the Degree dropdown was typed the applicant's full degree title, matched
 * nothing, and the combobox engine wiped the text again, which is what the
 * user saw as "it put it in then removed it". The neighbouring Discipline field
 * worked because deriveFieldOfStudy already reduces the same string.
 */
export function deriveDegreeLevel(degree: string): string | null {
  const d = (degree || "").trim();
  if (!d) return null;
  for (const [re, level] of DEGREE_LEVELS) {
    if (re.test(d)) return level;
  }
  return null;
}

/**
 * The one option that is a degree's own title: "Master of Science" for
 * "Master of Science in Information Systems" among Master of Arts / Master of
 * Science / … (Arcesium on Greenhouse, live, round 8), where the level
 * ("Master") names several and the field was left blank. The whole degree
 * first, then its title before " in <field>"; an option may add an
 * abbreviation ("Master of Science (M.S.)"). Null when none or several fit.
 */
export function degreeTitleOption(options: string[], degree: string): string | null {
  const key = (s: string): string => normalize(s.replace(/\([^)]*\)/g, " "));
  const whole = (degree || "").trim();
  if (!whole) return null;
  const title = whole.split(/\s+in\s+|,/i)[0].trim();
  for (const want of [whole, title]) {
    const k = key(want);
    if (!k) continue;
    const hits = options.filter((o) => key(o) === k);
    if (hits.length === 1) return hits[0];
  }
  // A certificate or diploma on a list of degrees that names none: the list's
  // "Other" (decision 96; Concordia's certificate and a bootcamp's on
  // Greenhouse, live, round 8, were left blank).
  const level = deriveDegreeLevel(whole);
  if ((level === "Certificate" || level === "Diploma") && !options.some((o) => /\b(certificate|diploma|certification)\b/i.test(o))) {
    const other = options.filter((o) => /^other\b/i.test(o.trim()));
    if (other.length === 1) return other[0];
  }
  return null;
}

/** The education entry a field answers for: its row's, or outside a row the
 *  entry in progress (else the most recent one), not merely the first. */
export function educationEntryFor(profile: UserApplicationProfile, groupIndex: number | null): NonNullable<UserApplicationProfile["education"]>[number] | undefined {
  if (groupIndex !== null) return profile.education?.[groupIndex];
  const primary = profileFacts(profile).education.primary;
  return primary
    ? profile.education?.find((e) => (e.school || "").trim() === primary.school && (e.degree || "").trim() === primary.degree) ?? profile.education?.[0]
    : profile.education?.[0];
}

/** Controls that force the answer to be one of a fixed set of options. */
const CHOICE_CONTROLS: ControlType[] = ["select", "combobox", "radioGroup", "checkboxGroup", "customDropdown"];

export const LONG_TEXT: ControlType[] = ["textarea", "contenteditable"];

/**
 * Map a classified field to the profile value it should receive.
 * Returns null when the profile has no usable data (the field then shows up
 * in the review panel instead of being silently skipped).
 */
export function resolveProfileValue(
  category: FieldCategory,
  profile: UserApplicationProfile,
  control: ResolveControl,
  // Retained for signature stability across the scanner/adapter call chain. EEO
  // now fills on data-presence (see the EEO cases below), so this no longer gates.
  _fillEEO: boolean
): string | null {
  const orNull = (v: string | undefined): string | null => (v && v.trim() ? v : null);
  const gi = control.groupIndex ?? null;
  // Derived facts (profileFacts.ts): only HIGH-confidence facts are used, so a
  // value this returns is one the profile settles, never a best guess.
  const facts = profileFacts(profile);
  const loc = facts.location;
  // Outside a repeating row, "your school" is the entry in progress (or the
  // most recent one), not merely the first one listed.
  const edu = educationEntryFor(profile, gi);

  switch (category) {
    case "firstName":
      return orNull(profile.firstName);
    case "lastName":
      return orNull(profile.lastName);
    case "fullName":
      return orNull([profile.firstName, profile.lastName].filter(Boolean).join(" "));
    case "email":
      return orNull(profile.email);
    case "phone":
      return orNull(profile.phone);
    // The phone satellites. Workday's adapter answers both, and a dialing-code
    // picker anywhere else is answered by the question rule (resolvePhoneCode,
    // the number's own country), so this is the never-reached-in-practice
    // fallback: the dialing prompt takes the country name ("Canada" → "Canada
    // (+1)"), the device type has no profile source and is left to the adapter/AI.
    case "phoneCountryCode":
      return orNull(profile.country);
    case "phoneDeviceType":
      return null;
    case "location": {
      const stated = profile.location?.trim() ?? "";
      // A bare city the address agrees with ("Gatineau", a real profile,
      // 2026-10-03) is completed from it below: alone it names two places on
      // Lever's typeahead (Quebec and Haiti), which then picks neither.
      const bareCity = Boolean(stated) && !stated.includes(",") && isHigh(loc.city) && loc.city.value.toLowerCase() === stated.toLowerCase();
      if (stated && !bareCity) return stated;
      // Composed from structured parts: "Toronto, ON, Canada".
      const parts = [
        isHigh(loc.city) ? loc.city.value : "",
        isHigh(loc.region) ? loc.region.value.code : "",
        isHigh(loc.country) ? loc.country.value.name : "",
      ].filter(Boolean);
      return parts.length >= 2 ? parts.join(", ") : stated || null;
    }
    case "addressStreet":
      // Only the street part: a one-line "1055 W Georgia St, Vancouver, BC…"
      // is split, and its city/region/postal code go to their own fields.
      return isHigh(loc.street) ? loc.street.value : null;
    case "addressCity":
      // The CITY, derived from the address or the location string. Never the
      // whole "Toronto, ON, Canada" string, which is what this used to write.
      return isHigh(loc.city) ? loc.city.value : null;
    case "addressState": {
      const stated = orNull(profile.addressState);
      // A dropdown searched by typing finds "Texas", never "TX" (Paylocity's
      // State, live 2026-10-05: left on "Select a state").
      if (stated && (control.controlType === "combobox" || control.controlType === "customDropdown") && !control.options?.length) {
        const home = countryFromName(profile.country ?? "")?.code;
        const region = regionFromText(stated, home === "US" || home === "CA" ? home : undefined);
        if (region) return region.name;
      }
      return stated ?? (isHigh(loc.region) ? loc.region.value.name : null);
    }
    case "postalCode":
      return isHigh(loc.postalCode) ? loc.postalCode.value : null;
    case "country": {
      // A country written short ("US", "USA") is its full name: a phone
      // picker's "United States +1" matched no "US" (Accenture Federal,
      // Epic Games; live 2026-10-05). A name the country list lacks stays.
      const stated = orNull(profile.country);
      if (stated) return countryFromName(stated)?.name ?? stated;
      return isHigh(loc.country) ? loc.country.value.name : null;
    }
    case "linkedin":
      return orNull(profile.linkedin);
    case "github":
      return orNull(profile.github);
    case "portfolio":
      return orNull(profile.portfolio);
    case "currentCompany":
      // Outside a repeating row: the stated current company, else the employer
      // of the one experience row that is still running ("Present").
      if (gi !== null) return orNull(profile.experience?.[gi]?.company);
      return isHigh(facts.employment.currentCompany) ? facts.employment.currentCompany.value : null;
    case "currentTitle":
      // Greenhouse's "Current role" is a CHECKBOX ("this is my current role"),
      // sharing a label with the job-title text field but asking a different
      // question. Writing a title string into it can only fail as an ambiguous
      // checkbox value, so answer it the way experienceCurrent does.
      if (control.controlType === "checkbox") return currentRoleFlag(profile, gi);
      if (gi !== null) return orNull(profile.experience?.[gi]?.title);
      return isHigh(facts.employment.currentTitle) ? facts.employment.currentTitle.value : null;
    // Experience dates / description only resolve inside a repeating row (gi set),
    // so a standalone "Start Date" (availability) never pulls an employment date.
    case "experienceStartDate":
      return orNull(gi !== null ? profile.experience?.[gi]?.startDate : undefined);
    case "experienceEndDate":
      return orNull(gi !== null ? profile.experience?.[gi]?.endDate : undefined);
    case "experienceDescription":
      return orNull(gi !== null ? profile.experience?.[gi]?.description : undefined);
    case "experienceCurrent":
      return currentRoleFlag(profile, gi);
    case "salary":
      return orNull(profile.salaryExpectation);
    case "skills":
      // Joined for display / single-text fields; a multi-select combobox splits
      // this back into one chip per skill (see comboboxEngine).
      return orNull(profile.skills?.filter((s) => s.trim()).join(", "));
    case "school": {
      // "School Type" over High School / Community College / College /
      // University (Paylocity's education row, live 2026-10-03): the kind of
      // school, never its name.
      const kind = schoolTypeOption(edu, control);
      return kind !== undefined ? kind : orNull(edu?.school);
    }
    case "degree": {
      // "Did you Graduate?" in an education row (Paylocity): that row's own
      // status, from its graduation date. Only in a row: outside one, a yes/no
      // on a degree is a requirement ("Bachelor's in Computer Science …
      // strongly preferred", FSSI on Workable), which a finished degree in
      // another field does not meet.
      if (gi !== null && hasYesNoOptions(control.options)) {
        const row = edu ? facts.education.entries.find((e) => e.school === (edu.school || "").trim() && e.degree === (edu.degree || "").trim()) : undefined;
        return row?.completed === true ? "Yes" : row?.completed === false ? "No" : null;
      }
      const d = orNull(edu?.degree);
      if (!d) return null;
      // A Degree DROPDOWN offers levels ("Bachelor's Degree"); a full degree
      // title matches none of them. A free-text Degree field wants the title.
      // A list of titles ("Master of Arts", "Master of Science", …) takes the
      // row's own title, which the level alone cannot tell apart.
      if (CHOICE_CONTROLS.includes(control.controlType)) return (control.options?.length ? degreeTitleOption(control.options, d) : null) ?? deriveDegreeLevel(d) ?? d;
      return d;
    }
    case "fieldOfStudy":
      return edu?.degree ? deriveFieldOfStudy(edu.degree) : null;
    case "graduationYear":
      return orNull(edu?.graduationYear);
    case "education":
      return formatEducation(profile);

    case "workAuthorization": {
      const v = orNull(profile.workAuthorization);
      if (!v) return null;
      // A Yes/No answer depends on WHICH country the question asks about, and
      // "Authorized to work in Canada" says nothing about the United States.
      // That judgment belongs to questionResolver (it reads the country and
      // abstains when the profile does not cover it); reaching here with a
      // choice control means it did not recognize the question, so abstain
      // rather than turn the statement into a country-blind "Yes".
      return isYesNoChoice(control) ? null : v;
    }
    case "sponsorship": {
      const v = orNull(profile.requiresSponsorship);
      if (!v) return null;
      return isYesNoChoice(control) ? null : v;
    }

    // Screening answers stated once on the profile. Returned RAW: a constrained
    // control (select / radio / checkbox group) runs the value through
    // guardConstrainedOption in formScanner, and a combobox through
    // comboboxEngine, both use matchOption, so "Yes" lands on "Yes, I am
    // willing to relocate" and "5" lands in a "5-7 years" bucket. A value that
    // matches no option is dropped there rather than written raw.
    case "willingToRelocate": {
      const v = orNull(profile.willingToRelocate);
      if (!v) return null;
      // Stored as "Yes"/"No", but a hand-typed sentence still answers a Yes/No
      // control correctly rather than being written into it verbatim.
      return isYesNoChoice(control) ? toYesNo(v) : v;
    }
    case "driversLicense": {
      const v = orNull(profile.driversLicense);
      if (!v) return null;
      return isYesNoChoice(control) ? toYesNo(v) : v;
    }
    case "workPreference":
      return orNull(profile.workPreference);
    case "noticePeriod":
      return orNull(profile.noticePeriod);
    case "startDate":
      return orNull(profile.earliestStartDate);
    case "yearsOfExperience":
      return orNull(profile.yearsOfExperience);
    case "securityClearance":
      return orNull(profile.securityClearance);
    case "languages":
      return orNull(profile.languages);

    case "coverLetter":
      // Only into long-text controls; a cover-letter *file* input can't be filled.
      return LONG_TEXT.includes(control.controlType) ? orNull(profile.coverLetter) : null;
    case "experience":
      // Free-form summaries only: a "years of experience" number we don't have.
      return LONG_TEXT.includes(control.controlType) ? formatExperience(profile) : null;

    case "resumeUpload":
      return null; // browsers do not allow scripted file selection

    // EEO / demographics: the user entered these in their own profile expressly
    // to autofill applications, so the presence of an answer IS the consent to
    // fill it, resolved whenever set (empty → null; never fabricated). These
    // stay `sensitive`, so the AI never guesses them and they're clearly flagged
    // for review, and the user can still deselect any before filling.
    case "eeoGender":
      return orNull(profile.eeo?.gender);
    case "eeoGenderIdentity":
      // Falls back to the plain gender answer when no distinct identity is set.
      return orNull(profile.eeo?.genderIdentity || profile.eeo?.gender);
    case "eeoPronouns":
      // No fallback: pronouns are not derivable from a gender answer.
      return orNull(profile.eeo?.pronouns);
    case "eeoRace":
      return orNull(profile.eeo?.race);
    case "eeoHispanic":
      return orNull(profile.eeo?.hispanicLatino);
    case "eeoVeteran":
      return orNull(profile.eeo?.veteranStatus);
    case "eeoDisability":
      return orNull(profile.eeo?.disabilityStatus);
    case "eeoSexualOrientation":
      return orNull(profile.eeo?.sexualOrientation);
    case "eeoOther":
      return null;
    case "signatureDate": {
      // Today, written without an order to misread: a box that shows its
      // format re-emits it in that format (fieldResolver). Month first, it
      // was read day first by Workable's "DD/MM/YYYY" boxes: 8 October went
      // in as 10 August (Saalex, live 2026-10-08).
      const d = new Date();
      const pad = (n: number): string => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    }

    // Account signup password: never resolved from the profile; it is written
    // only by the account sub-flow, never generically.
    case "accountPassword":
    case "unknown":
      return null;
  }
}
