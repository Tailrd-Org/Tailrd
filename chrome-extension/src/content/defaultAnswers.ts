/**
 * DEFAULT ANSWERS: screening questions no profile fact settles, but that a
 * typical applicant answers the same way, answered on-device so they never
 * wait for the AI (which is for prose and judgement, and may be unavailable).
 *
 * The working assumption is an applicant who ACCEPTS the posting's standard
 * terms (they are applying to it) and is UNENCUMBERED (no prior application,
 * no relative inside, no conflict, no non-compete). Every rule names that
 * assumption in its `rule` id, so telemetry and the review panel show which
 * answer came from a default rather than from the profile.
 *
 * What is deliberately NOT defaulted, because a wrong answer there is a false
 * statement with consequences: work authorization, sponsorship, citizenship
 * and export-control status (profile facts only, see questionResolver),
 * criminal history, and anything asking for a fact (dates, numbers, names).
 * Marketing / SMS / newsletter opt-ins are answered NO.
 *
 * Runs LAST in resolveQuestion: a profile fact always wins over a default.
 */
import type { UserApplicationProfile } from "../shared/types";
import { isConsentOption, optionPolarity } from "./answerKind";
import { isHigh, type ProfileFacts } from "./profileFacts";
import { placeOf } from "./placeMatch";
import { REGION_CITIES, regionFromText, regionHintForCity } from "./geo";
import { askedOfStatement, sentencesOf } from "./statementText";
import type { QuestionContext, QuestionInput, QuestionResult } from "./questionResolver";

const answer = (value: string, rule: string): QuestionResult => ({ status: "answer", value, confidence: "high", rule });
/** A government body in the history: "a current or former government
 *  employee?" is the applicant's to judge (is the Army one? the employer test
 *  is broad on purpose), and the AI, with the same history, could only guess
 *  (a veteran on ActioNet's Jobvite form, live 2026-10-03). */
const GOVERNMENT_HISTORY: QuestionResult = { status: "abstain", rule: "default:government-history", blockBackend: true };
const RECORDING_CONSENT: QuestionResult = { status: "abstain", rule: "default:recording-consent", blockBackend: true };
/** Who referred the applicant: theirs to write, never a guess. */
const REFERRER_UNKNOWN: QuestionResult = { status: "abstain", rule: "default:referrer-unknown", blockBackend: true };
/** Who wrote the application, or whether an AI helped: Tailrd may have. */
const AUTHORSHIP =
  /\b(this|the|my|your) application\b[^.?]{0,40}\b(myself|yourself|by a person|by a human|personally|on my own)\b|\bcompleted by a (person|human)\b|\b(used|using|use) an? (ai|artificial intelligence) tool\b|\bai (generated|assisted|written)\b/;
const AUTHORSHIP_THEIRS: QuestionResult = { status: "abstain", rule: "default:authorship", blockBackend: true };
/** Needing an accommodation, for anyone who has not stated "no disability". */
const ACCOMMODATION_THEIRS: QuestionResult = { status: "abstain", rule: "default:accommodation-theirs", blockBackend: true };
/**
 * Giving up the right to sue: agreeing to arbitrate, or waiving a jury trial
 * or a class action (decision 51). Receiving the agreement, or reading it,
 * gives nothing up ("Please confirm receipt of … US Arbitration Agreement.",
 * Coinbase; "I will read the arbitration agreement below.", Anthropic).
 * Matched on normalized words, so the windows count words.
 */
const WAIVES_RIGHTS =
  /\b(agree|agrees|agreed|agreeing|accept|accepts|accepted|consent|consents|bound)\b(?: \w+){0,25} arbitrat\w*|\barbitrat\w*(?: \w+){0,25} (agree|agrees|agreed|accept|accepts|accepted|bound)\b|\bwaiv\w*(?: \w+){0,8} (jury|class|collective|right to sue)\b|\b(jury trial|class action|collective action)s?(?: \w+){0,6} waiv\w*/;
const RIGHTS_THEIRS: QuestionResult = { status: "abstain", rule: "default:waives-rights", blockBackend: true };

/** Lowercase words (mirrors questionResolver.qnorm, kept local to avoid an import cycle). */
const qn = (text: string): string =>
  (text || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[’']/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9+]+/g, " ")
    .trim();

const realOptions = (q: QuestionInput): string[] =>
  (q.options ?? []).filter((o) => o.trim() && !/^(select|choose|please select|select an option|select one|--)/i.test(o.trim()));

/**
 * A computed yes/no, rendered into what the field takes. Options known: the ONE
 * option of that polarity (or a bare Yes/No among several). Options unknown (a
 * react-select before it opens): "Yes"/"No", snapped when the options arrive.
 */
function polar(value: boolean, q: QuestionInput, rule: string): QuestionResult {
  const opts = realOptions(q);
  if (opts.length > 0) {
    const same = opts.filter((o) => optionPolarity(o) === value);
    if (same.length === 1) return answer(same[0], rule);
    const bare = same.filter((o) => /^(yes|no)$/i.test(o.trim()));
    if (bare.length === 1) return answer(bare[0], rule);
    return null; // several options share the polarity: a chooser below may know which
  }
  if (q.controlType === "checkbox") return answer(value ? "yes" : "no", rule);
  if (q.kind === "boolean" || q.controlType === "combobox" || q.controlType === "customDropdown" || q.controlType === "select" || q.controlType === "radioGroup" || q.controlType === "ariaRadioGroup") {
    return answer(value ? "Yes" : "No", rule);
  }
  return null; // free text: a bare Yes would not read as an answer to most of these
}

// ---------------------------------------------------------------------------
// The families. Order matters: opt-outs before consents ("I agree to receive
// marketing emails" is marketing), conditions before acknowledgements.
// ---------------------------------------------------------------------------

/** Opt-ins nobody needs to apply: answered NO. */
const MARKETING =
  /\b(marketing|newsletters?|subscribe|promotional|sms|text messages?|texts? (me|messages)|(via|by) text|text (communications?|updates|alerts)|whats ?app|job alerts?|mailing list|keep (me )?(in touch|informed)|stay (in touch|informed)|receive (news|emails?|communications?|updates?|information|messages|notifications?))\b/;

/** Being kept on file for other roles: harmless and in the applicant's interest. */
const FUTURE_ROLES = /\b(future (job |career |employment )?(opportunities|openings|roles|positions|vacancies)|other (opportunities|roles|positions|openings)|talent (community|network|pool|pipeline)|consider(ed)? (me )?for (other|future))\b/;

/** Consent / certification an application cannot be submitted without. */
const CONSENT_VERB =
  /\b(consent|agree|accept|acknowledge|understand|confirm|certify|attest|declare|authori[sz]e)\b|\bi have read\b|\bby (checking|selecting|clicking|submitting)\b/;
const CONSENT_OBJECT =
  /\b(privacy|personal (data|information)|data (protection|processing|privacy)|gdpr|ccpa|processing|terms|conditions|polic(y|ies)|notice|statement|disclosure|disclaimers?|accurate|true|truthful|complete|correct|falsif\w*|misrepresent\w*|omission|retain|retention|stor(e|ing)|collect(ion|ing)?|candidate (data|information)|applicant (data|information))\b/;
/** Demographic-data consent belongs to the EEO path (sensitive), never defaulted. */
const DEMOGRAPHIC = /\b(demographic|self ?identif\w*|eeo|equal employment|voluntary (self|disclosure))\b/;
/** Recording / AI notetaking consent: a preference, not a requirement. */
const RECORDING = /\b(record(ed|ing)?|transcri\w*|notetak\w*|note tak\w*)\b/;
/** The APPLICANT is the one recorded or transcribed. */
const APPLICANT_RECORDED =
  /\b(we|our team|our interviewers)\b[^.?!]{0,60}\b(record|transcri)\w*|\b(record|transcri)\w* (your|the|my) (interview|video|call|conversation)s?\b|\bvideo recording\b|\binterviews? (being |will be |may be |is |are )?(record|transcri)\w*|\bnotetak\w*|\bnote tak\w*/;

/** A requirement of the posting the applicant accepts by applying. */
// The third person too: "The candidate acknowledges that…" (Coveo, live 2026-10-05).
const ACK_VERB =
  /\b(acknowledges?|agrees?|understands?|confirms?|accepts?|comfortable|okay|ok|willing|able|open|prepared|available|can you|will you be able|are you able|works? for you)\b/;
const REQUIREMENT =
  /\b(in ?office|on ?site|in ?person|hybrid|office|commut\w*|travel\w*|relocat\w*|shifts?|weekends?|overtime|nights?|evenings?|holidays?|background (check|screen\w*|investigation)s?|drug (test\w*|screen\w*)|essential (functions|duties)|lift\w*|schedule|days (per|a|each) week|hours (per|a|each) week|requirement|requirements|move to the location|moving to the location)\b/;
/** A request rather than a requirement ("Will you require relocation
 *  assistance?"): the applicant's to make, never defaulted. */
// "Support" is help only as a noun of it ("relocation support"): "travel as
// needed to support these activities" (Kobie on Lever, live 2026-10-08) asks
// the requirement.
const ASSISTANCE = /\b(assistance|package|(relocation|moving|travel|visa|immigration|financial) support|support (for|with) (your |the )?(relocation|move|moving|visa)|expenses?|benefits?|allowance|reimburs\w*|stipend|bonus|housing)\b/;
/** Pay and what comes with it: the applicant's to accept, wherever the
 *  question mentions it. */
const PAY_TERMS = /\b(compensation|pay|salary|salaries|wages?|hourly rate|benefits?|bonus(es)?|stipend|reimburs\w*)\b/;
/** A question about the applicant's history, never a requirement or an opt-in. */
const EXPERIENCE_ASKED =
  /\b(do|did) you have\b[^?]{0,20}\bexperience\b|\bhave you (ever )?(had|gained|managed|led|worked)\b|\bexperience (in|with|managing|leading|supporting|developing|working)\b/;
/** "…challenges clearing a background check?": the clean answer is NO. */
const OBSTACLE = /\b(challenges?|issues?|problems?|concerns?|difficult(y|ies)?|prevent you|preclude|disqualif\w*|impediments?|barriers?|obstacles?|restrictions?|limitations?)\b/;

const PRIOR_APPLICATION =
  /\b(have|did) you (ever |previously |already )?(applied|interviewed|submitted (an )?application|filed (an )?application)\b|\bpreviously (applied|interviewed)\b|\bapplied (here|before|previously)\b|\binterviewed (here|before|with us)\b/;
/** "Were you referred by an employee?" (a yes/no; WHO referred stays unanswerable). */
const REFERRED = /\b(were|was) you referred\b|\bwere you referred by\b|\breferred by (a|an|any) (current )?(employee|staff|team member|friend)\b|\bemployee referral\b/;
const RELATIVES =
  /\b(relatives?|family members?|related to|spouse|domestic partner|immediate family|household members?|familial|personal relationships?)\b/;
const CONFLICTS = /\bconflicts? of interest\b|\boutside (business|employment) (activit|interest)\w*/;
/** A statement affirming the NONE ("Affirmation: I affirm that I have not
 *  entered into any non-competition, non-solicitation, non-disclosure/
 *  confidentiality … agreement with a former employer", Saalex on Workable,
 *  2026-10-05): the default "No" would deny it, and a confidentiality
 *  agreement is common. The applicant's own to affirm. */
const AFFIRMS_NONE =
  /\bi (hereby )?(affirm|certify|confirm|attest|declare|represent|warrant)\b[^.?]{0,20}\b(have not|have never|am not|do not|never|have no)\b/;
// "…employment agreements and/or post-employment restrictions…" (GitLab, live 2026-10-05).
const NON_COMPETE =
  /\bnon ?compet\w*|\bnon ?solicit\w*|\brestrictive covenants?\b|\b(agreements?|contracts?) that (would |may |might )?(restrict|prevent|limit|prohibit)\b|\bpost ?employment (restrictions?|obligations?|covenants?)\b/;
const GOV_OFFICIAL =
  /\b(government|public|foreign) officials?\b|\bprocurement officials?\b|\bpolitically exposed\b|\b(current|former|currently|formerly)\b[^?]{0,30}\b(government|federal|state|public sector) officials?\b/;
// "…a current employee of the U.S. Government (including U.S. Congress or
// military) or any state or local government?" (Accenture Federal, live
// 2026-10-05) as well as "a current or former government employee".
const GOV_EMPLOYEE =
  /\b(current|former|currently|formerly)\b[^?]{0,30}\b(government|federal|state|public sector) employees?\b|\b(an )?employee of (the )?(u ?s |united states |federal |state |local )?(government|congress|military)\b/;
/** An employer that is a government body ("Public Services and Procurement
 *  Canada", "City of Ottawa", "U.S. Department of Energy"). Broad on purpose: a
 *  false hit only leaves the question to the applicant. */
const GOV_EMPLOYER =
  /\b(government|gouvernement|ministry|ministere|department of|dept of|federal|provincial|municipal|municipality|city of|town of|county of|region of|province of|state of|public services?|public sector|procurement|parliament|senate|house of commons|legislative|legislature|treasury|revenue agency|border services|armed forces|army|navy|air force|marine corps|coast guard|national guard|police|rcmp|crown corporation)\b|\b(health|transport|statistics|service|parks|environment|justice|finance|heritage|agriculture|immigration|fisheries|infrastructure|natural resources|global affairs|veterans affairs|public safety|indigenous services|shared services|library and archives|elections) canada\b|\bcanada (revenue|border|post)\b|\b(nasa|noaa|usps|fbi|cia|nsa)\b|\b(u s|united states) (department|army|navy|air force|government)\b/;
/** A title naming an official's role, not just a job at a government body. */
const OFFICIAL_TITLE = /\b(official|officer|minister|commissioner|contracting|procurement|purchasing|buyer|director general)\b/;

/**
 * The applicant's history with government: an employer that is one, and a
 * title there that names an official's role. "Are you a current or former
 * government employee?" was answered No for a profile whose last job was a
 * federal internship (ActioNet on Jobvite, a real profile, 2026-10-03).
 */
function governmentHistory(profile: UserApplicationProfile): { employer: boolean; official: boolean } {
  const isGov = (company: string): boolean => {
    const c = qn(company);
    return Boolean(c) && GOV_EMPLOYER.test(c) && !/\b(university|college|hospital)\b/.test(c);
  };
  const rows = (profile.experience ?? []).filter((r) => r && isGov(r.company ?? ""));
  return {
    employer: rows.length > 0 || isGov(profile.currentCompany ?? ""),
    official: rows.some((r) => OFFICIAL_TITLE.test(qn(r.title ?? ""))),
  };
}

/** The "never a government employee / official" default, unless the history says otherwise. */
function notGovernment(n: string, profile: UserApplicationProfile): boolean | null {
  const employee = GOV_EMPLOYEE.test(n);
  if (!employee && !GOV_OFFICIAL.test(n)) return null;
  const gov = governmentHistory(profile);
  // An OFFICIAL is a role, not an employer: a developer intern at a
  // department is a government employee, and still no procurement official.
  return !(employee ? gov.employer : gov.official);
}
const CRIMINAL = /\b(criminal|convicted|conviction|felony|misdemeanou?r|arrested|charged with|pending charges)\b/;
/** "Preferred Contact Method", "method of contact", "how would you prefer we contact you". */
const CONTACT_METHOD = /\b(contact|communication) (method|preference|channel)s?\b|\b(method|means|mode|way) of (contact|communication)\b|\bprefer\w* (we|us|to be) (contact|reach)\w*|\bbest way to (contact|reach) you\b/;

const HOW_HEARD =
  // "Where did you first see this position?" (Iambic), "How'd you hear about
  // Range?" (Ashby bank 2026-10-08).
  /\bhow (did )?you (first |originally )?(hear|heard|find|found|learn|learned|come across|came across|discover|discovered|connect|connected)\b|\bwhere did you (first |originally )?(hear|see|find|learn|come across|discover)\b|\bhowd you (first )?(hear|find|learn|discover)\b|\bhow were you (referred|introduced)\b|\b(referral|application|applicant|candidate|job) source\b|\bsource of (application|referral)\b|\bhow did you get to know\b|\b(which|what) (job |career )?(site|board|website|platform)\b[^?]{0,30}\b(see|saw|find|found|hear|heard|learn|discover)\b/;
// ("From which job site did you see this posting?", Data Lab on Lever, live
// 2026-10-08, the last alternative.)

/** Channels a "how did you hear" list offers; three or more in an unlabeled
 *  question ("Select One", Hermeus on Lever) make it that question. */
const CHANNEL = /\b(linked ?in|indeed|glassdoor|company (website|site)|careers? (page|site|website)|referral|career (fair|services)|job board|facebook|twitter|instagram|youtube|built ?in|handshake|recruiter|google)\b/;
const UNLABELED = /^(select|choose|pick)( one| an option| all that apply)?$|^$/;

/** An acknowledgement option: the only thing a form lets you answer. */
/** "Acknowledge/Confirm" (OneTrust's "Data Protection Notice", question bank
 *  2026-10-05) too; a bare "Accept" stays out (Block's arbitration). */
const ACK_OPTION = /^(i (will|understand|agree|acknowledge|confirm|accept|have read|consent)|acknowledged?|agree|agreed|understood|confirm(ed)?)\b/;
/** A titled policy / agreement with a Yes/No ("AI Policy for Application", Anthropic). */
const TITLED_TERMS = /\b(polic(y|ies)|agreement|terms|notice|acknowledg\w*|attestation|arbitration)\b/;

/** Options a "how did you hear" answer may take, most truthful first for a
 *  Tailrd user (who found the posting through a job aggregator). */
const SOURCE_PREFERENCE: RegExp[] = [
  /\bjob (board|site|search|posting|aggregator)s?\b|\bonline (job|posting|ad|listing)s?\b|\bjob search (engine|site|website)\b/,
  /\b(company|corporate|our) (website|site|careers? (page|site))\b|\bcareers? (page|site|website)\b|\bwebsite\b/,
  /\b(internet|online|web|search engine|google)\b/,
  /\blinked ?in\b/,
  // A named job board is still "found it on a job board".
  /\b(indeed|glassdoor|zip ?recruiter|monster|simply ?hired|dice|built ?in|wellfound|angel ?list|handshake|job ?bank|workopolis|eluta|talent com|jobillico|levels fyi)\b/,
  /\bother\b/,
];

/** Working in a place in person (an office, on site, a hybrid schedule). */
/** "work out of our Wakefield, MA office" too (Sentinel on JazzHR, regression
 *  2026-10-05: Yes from Austin for someone who will not move). */
// "come into the Wakefield, MA office five days per week" too (Vestmark,
// question bank 2026-10-05: Yes from Seattle for someone who will not move).
// "Are you comfortable commuting to our office…" (Financeit on Workable,
// 2026-10-05) was not read as commuting: Yes from Seattle.
// "…work from our Foster City, CA HQ 3 days per week?" (Replit, Ashby bank
// 2026-10-08) names no office: a headquarters or campus is one.
const IN_PERSON =
  /\b(in ?office|on ?site|in ?person|hybrid|report to (the|our) office)\b|\b(work|working|commute|commuting|report|reporting)\b[^?]{0,60}\b(from|at|in|to|out of)\b[^?]{0,40}\b(offices?|hq|headquarters|campus)\b|\b(this|the|that|a daily) commute\b|\b(come|go|be) (in|into|to)\b[^?]{0,40}\b(offices?|hq|headquarters|campus)\b/;

/** Being there now and then rather than working there: team gatherings, an
 *  offsite, a few trips a year. Someone who will not relocate can still go. */
const OCCASIONAL_PRESENCE =
  /\b(occasional(ly)?|periodic(ally)?|from time to time|as needed|((a few|several|\d+|one|two|three|four) times|once|twice) (a|per|each) (year|quarter)|quarterly|annual(ly)?|yearly|off ?sites?|retreats?|gatherings?|summits?|meetups?|team (events?|weeks?)|company (events?|meetings?)|visits?|trips?)\b/;
/** A schedule that makes it regular work after all ("hybrid, three days a
 *  week, with quarterly offsites"; "25% of the time"). */
const REGULAR_PRESENCE =
  /\b(hybrid|full ?time|daily|every (day|week)|weekly|regular(ly)?|(\d|one|two|three|four|five) days? (a|per|each|every) week|days (a|per|each) week)\b|\d+ ?% of (the|your) time/;

/** Research or outside funding that could claim the work (Zoox on Lever). */
const RESEARCH_OR_FUNDING =
  /^(are|do) you (currently )?(conducting|doing|performing|engaged in) (any )?research\b|^do you (currently )?(receive|have|hold) (any )?(active )?(funding|grants?|sponsorships?)\b/;

/** The places a question names ("out of Pleasant Grove, Utah", "in Austin, TX",
 *  "in our Mountain View, CA headquarters", an address's "…, New York, NY"). */
function placesInLabel(label: string): ReturnType<typeof placeOf>[] {
  const out: ReturnType<typeof placeOf>[] = [];
  const add = (text: string) => {
    // The sentence ends at its full stop: "located in Manville, NJ. Does this
    // commute…" read "Manville, NJ. Does" and found no state (Carvana,
    // question bank 2026-10-05).
    const place = placeOf(text.split(/\.\s/)[0].replace(/[.,;:]+$/, ""));
    if (place.city && (place.region || place.country) && !out.some((p) => p.city === place.city && p.region === place.region)) out.push(place);
  };
  // "in our Mountain View, CA headquarters" (Nuro, question bank 2026-10-05).
  for (const m of label.matchAll(/\b(?:out of|in|at|from|based in|located in)\s+(?:(?:our|the|either|its|their)\s+)?([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3},\s*[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})/g)) add(m[1]);
  // A city and its state anywhere else: the office's address "(located at
  // 441 9th Avenue, New York, NY)" (Peloton, question bank 2026-10-05).
  for (const m of label.matchAll(/\b([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3}),\s*([A-Z]{2})\b/g)) add(`${m[1]}, ${m[2]}`);
  // Without the comma: "work onsite in Whippany NJ" (Giftogram, question bank
  // 2026-10-05). Only a real state or province code after the city.
  for (const m of label.matchAll(/\b(?:in|at|near|out of|from)\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+([A-Z]{2})\b/g)) {
    if (regionFromText(m[2], "US") || regionFromText(m[2], "CA")) add(`${m[1]}, ${m[2]}`);
  }
  // A well-known office city named alone: "our Austin office" (Cloudflare,
  // question bank 2026-10-05), "onsite in Austin".
  for (const m of label.matchAll(OFFICE_CITY)) {
    const region = regionHintForCity(m[1]);
    if (region) add(`${m[1]}, ${region}`);
  }
  return out;
}

/**
 * Working in person somewhere, for an applicant who will not relocate: their
 * own city ("local"), only other states or countries ("elsewhere"), places
 * that are neither ("unclear"), or null when this does not apply (no
 * in-person work asked, they would move, or no place is named or posted).
 */
export function onsiteVerdict(
  label: string,
  profile: UserApplicationProfile,
  facts: ProfileFacts,
  ctx: QuestionContext
): "local" | "elsewhere" | "unclear" | null {
  if (!IN_PERSON.test(qn(label)) || !/^no\b/i.test((profile.willingToRelocate ?? "").trim())) return null;
  const named = placesInLabel(label);
  const posted = (ctx.jobPlaces ?? []).map((p) => placeOf(p)).filter((p) => p.city && (p.region || p.country));
  const places = named.length > 0 ? named : posted;
  if (places.length === 0) return null;
  const home = facts.location;
  const homeCity = isHigh(home.city) ? qn(home.city.value) : null;
  if (homeCity && places.some((p) => p.city === homeCity)) return "local";
  const homeRegion = isHigh(home.region) ? `${home.region.value.country}:${home.region.value.code}` : null;
  const homeCountry = isHigh(home.country) ? home.country.value.code : null;
  const elsewhere = (p: ReturnType<typeof placeOf>) =>
    Boolean((p.country && homeCountry && p.country !== homeCountry) || (p.region && homeRegion && p.region !== homeRegion));
  return places.every(elsewhere) ? "elsewhere" : "unclear";
}

/** A well-known office city after "our" / "in" (the names are plain words). */
const OFFICE_CITY = new RegExp(
  // Not before ", ST" (the comma pattern has those); "hybrid in NYC, 2 days per
  // week" still names NYC (AssemblyAI, question bank 2026-10-05).
  `\\b(?:our|the|their|its|in|at|near|out of)\\s+(?:(?:downtown|midtown|uptown|central)\\s+)?(${REGION_CITIES.join("|")})\\b(?!,\\s*[a-z]{2}\\b)`,
  "gi"
);

/** A channel named for a brand ("LinkedIn Job Search", "Glassdoor Article"). */
const NAMED_SOURCE =
  /\b(linked ?in|indeed|glassdoor|zip ?recruiter|monster|simply ?hired|dice|built ?in|wellfound|angel ?list|handshake|otta|facebook|instagram|twitter|tiktok|youtube|reddit|career ?builder)\b/;

/** Map the applicant's stated source (profile.howDidYouHear) onto option words. */
const SOURCE_SYNONYMS: Array<[RegExp, RegExp]> = [
  [/\blinked ?in\b/, /\blinked ?in\b/],
  [/\bjob (board|site)\b|\bindeed\b|\bglassdoor\b|\bonline\b/, /\bjob (board|site|search|posting)s?\b|\bonline\b|\bindeed\b|\bglassdoor\b|\binternet\b/],
  [/\b(company|careers?) (website|site|page)\b|\bwebsite\b/, /\b(company|corporate|careers?|our) (website|site|page)\b|\bwebsite\b/],
  // "I know someone that works at Affirm" (question bank 2026-10-05);
  // "Connection in the Company". An option naming an employee alone ("Former
  // Employee", Accenture Federal; "Current/Former Employee", Roku; "Current
  // Fox Employee", Tubi) says who the APPLICANT is, never a referral.
  [/\breferr\w*|\bemployee\b|\bfriend\b/, /\breferr\w*|\bfriend\b|\bknow someone\b|\bsomeone (who|that) works\b|\b(connection|contact)s? (in|at|within) the company\b/],
  // Then an unqualified "Appian Employee" (heard from one); a current, former
  // or ex- employee is the applicant, and a "LinkedIn Employee Post" a channel.
  // An employees' event is a channel, not a referral: "Employee Presentation"
  // for a stated referral (Match Group on Lever, question bank 2026-10-08).
  [/\breferr\w*|\bemployee\b|\bfriend\b/, /^(?!.*\b(former|current|ex|previous|past|alumni|alumnus|post|posting|linked ?in|presentations?|events?|blog|webinars?|panels?|talks?|stories|story|spotlights?|testimonials?|newsletters?)\b).*\bemployees?\b/],
  [/\b(career|job) fair\b|\buniversity\b|\bcampus\b|\bschool\b|\bcollege\b/, /\b(career|job) fair\b|\buniversity\b|\bcampus\b|\bschool\b|\bcollege\b|\bco ?op\b/],
  // "Social media" is no particular platform: "Twitter" for it was a guess
  // (Twilio, Gusto's "Facebook"; question bank 2026-10-05). A platform
  // stated is that platform.
  [/\bsocial\b/, /\bsocial\b/],
  [/\btwitter\b|\bx\b/, /\btwitter\b|\bx\b/],
  [/\bfacebook\b|\bmeta\b/, /\bfacebook\b|\bmeta\b/],
  [/\binstagram\b/, /\binstagram\b/],
  [/\btik ?tok\b/, /\btik ?tok\b/],
  [/\bother\b/, /\bother\b/],
];

/**
 * An option naming one board ("Girls in Tech Job Board", "Indeed", "Dice"),
 * not the channel ("Job Board", "Online job board", "Job Board (Indeed,
 * Monster, etc.)": examples in brackets name no one board). A generic "Job
 * board" got Nutanix's "Girls in Tech Job Board" and ActioNet's "Indeed", and
 * "LinkedIn" the Girls in Tech board too (Jobvite, question bank 2026-10-17).
 */
function namesOneBoard(option: string): boolean {
  const raw = option.replace(/\([^)]*\)/g, " ");
  const t = qn(raw);
  // A board for one kind of people or place is that board, whatever else it
  // says ("Company Alumni Job Board or LinkedIn Group", "Diversity Job Board").
  if (/\b(alumni|university|campus|college|school|diversity|internal|government|state|regional|local|global)\b/.test(t)) return true;
  // The channel with brands as examples is the channel: "Indeed or other job
  // board" (Kobie), "Job posting on LinkedIn, Indeed, or other job board"
  // (Faire), "Job Board/Recruitment Platform" (BigID), "A different job
  // posting site" (Iambic).
  if (/\b(other|another|any|different)\b/.test(t) || (/\/|\b(or|and)\b/.test(raw) && /\bjob (board|site|search|posting)s?\b/.test(t))) return false;
  // A job bank is one government's board ("New York State Job Bank").
  if (NAMED_SOURCE.test(t) || /\bjob ?bank\b/.test(t)) return true;
  if (!/\b(board|site|website|search)s?\b/.test(t)) return false;
  const GENERIC = /^(job|jobs|career|careers|board|boards|site|sites|website|websites|search|searches|engine|online|internet|web|posting|postings|listing|listings|ad|ads|advert|advertisement|recruitment|recruiting|platform|platforms|aggregator|aggregators|portal|employment|an|a|the|or|and|third|party|general|public|external|e|g|etc|via|through|on)$/;
  return t.split(" ").some((w) => w && !GENERIC.test(w));
}

/** The brand an option or a stated source names ("indeed" for "Indeed.com"). */
function brandOf(text: string): string | null {
  return NAMED_SOURCE.exec(qn(text))?.[0].replace(/\s+/g, "") ?? null;
}

/** A company-website option that is the company's OWN site: its name, or a
 *  generic "Careers Website", never "NSBE Careers- National Society of Black
 *  Engineers Career Site" (Bandwidth, question bank 2026-10-05). */
function ownSite(option: string, company: string): boolean {
  // "Affirm’s Career Site", "Datadog's Careers Page": the possessive is the name.
  const words = (s: string): string[] => qn(s.replace(/['’]s\b/gi, "")).split(" ").filter(Boolean);
  // Connectors too: "Miter website or careers page" (Ashby bank 2026-10-08).
  const SITE = /^(company|corporate|careers?|carrieres?|our|the|official|website|web|site|page|jobs?|emplois?|portal|board|de|du|la|le|des|or|and|ou|et)$/;
  const rest = words(option).filter((w) => !SITE.test(w));
  // Every remaining word is the company's ("Appian Careers Website" for
  // "Appian Corporation", "Lucid Careers Page" for "Lucid Motors").
  const own = new Set(words(company));
  return rest.every((w) => own.has(w));
}

/**
 * The unencumbered applicant's "No" typed into a text box: "*Were you referred
 * to this job by a Mindex employee? If so, who?" (Workable), "Do you have a
 * family member/relative that currently works at ActioNet?" (Jobvite).
 */
function unencumberedText(q: QuestionInput, n: string, profile: UserApplicationProfile): QuestionResult {
  if (q.kind !== "text" && q.kind !== "longText") return null;
  if (!/^(are|were|was|have|has|had|do|did|is) you\b|^(are|were|have|do|did) (any|you)\b/.test(n)) return null;
  if (PRIOR_APPLICATION.test(n)) return answer("No", "default:no-prior-application");
  // A referral stated: by whom is the applicant's to write.
  if (REFERRED.test(n) || /^were you referred\b/.test(n)) {
    return /\breferr|\bemployee\b|\bfriend\b/.test(qn(profile.howDidYouHear ?? "")) ? null : answer("No", "default:not-referred");
  }
  if (RELATIVES.test(n) && /\b(work|employ|empl|staff|board|director|officer|relationship)\w*/.test(n)) return answer("No", "default:no-relatives-inside");
  if (CONFLICTS.test(n)) return answer("No", "default:no-conflict");
  if (NON_COMPETE.test(n)) return AFFIRMS_NONE.test(n) ? null : answer("No", "default:no-non-compete");
  const notGov = notGovernment(n, profile);
  if (notGov !== null) return notGov ? answer("No", "default:not-government-official") : GOVERNMENT_HISTORY;
  return null;
}

function chooseSource(q: QuestionInput, profile: UserApplicationProfile, company = ""): QuestionResult {
  const opts = realOptions(q);
  const stated = qn(profile.howDidYouHear ?? "");
  if (opts.length === 0) {
    // Free text, or a dropdown whose options arrive later (snapped then).
    if (stated) return answer(profile.howDidYouHear!.trim(), "source:stated");
    return answer(q.controlType === "text" || q.controlType === "textarea" ? "Online job board" : "Job board", "source:default");
  }
  const unique = (re: RegExp, keep: (o: string) => boolean = () => true): string | null => {
    const hits = opts.filter((o) => re.test(qn(o)) && keep(o));
    if (hits.length === 1) return hits[0];
    // "Job Board (Indeed, Monster, etc.)" over "University Job Board"
    // (Palantir on Lever): the general channel, when one is general.
    const general = hits.filter((o) => !/\b(university|campus|school|college|internal)\b/.test(qn(o)));
    return general.length === 1 ? general[0] : null;
  };
  if (stated) {
    const exact = opts.find((o) => qn(o) === stated);
    if (exact) return answer(exact, "source:stated");
    // A recruiter's outreach that mentions the channel ("A recruiter … contacted
    // me directly, such as via LinkedIn message") is not finding the job there
    // (Flipp, question bank 2026-10-05), unless a recruiter is what was said.
    const outreach = (o: string): boolean => /\brecruiter|\breached out\b|\bcontacted me\b|\boutreach\b|\bsourced\b/.test(qn(o)) && !/\brecruit/.test(stated);
    // A board the applicant did not name is never theirs (namesOneBoard).
    const otherBoard = (o: string): boolean => namesOneBoard(o) && !(brandOf(o) !== null && brandOf(o) === brandOf(stated));
    for (const [said, offered] of SOURCE_SYNONYMS) {
      if (!said.test(stated)) continue;
      const keep = /website/.test(said.source)
        ? (o: string) => ownSite(o, company) && !outreach(o)
        : /job \(board/.test(said.source)
          ? // A forum, a community or a blog online is no board ("Online
            // Forum or Community", Coveo).
            (o: string) => !outreach(o) && !otherBoard(o) && !/\b(forum|forums|communit\w*|groups?|blogs?|news|articles?|events?|webinars?|podcasts?)\b/.test(qn(o))
          : (o: string) => !outreach(o);
      const hit = unique(offered, keep);
      if (hit) return answer(hit, "source:stated");
      // Several of that kind ("Coveo Employee Referral" | "Friend or Former
      // Colleague", Coveo, live 2026-10-05): the one naming the stated word.
      const stem = said.exec(stated)?.[0];
      const own = stem ? opts.filter((o) => offered.test(qn(o)) && keep(o) && qn(o).includes(stem)) : [];
      if (own.length === 1) return answer(own[0], "source:stated");
      // Several of that channel ("LinkedIn Company Post" | "LinkedIn Employee
      // Post" | "LinkedIn Job Search", Planet on Greenhouse): a posting found
      // through Tailrd was found through a job search.
      const viaSearch = opts.filter((o) => offered.test(qn(o)) && keep(o) && /\bjob (search|post|posting|board|listing|ad)s?\b/.test(qn(o)));
      if (viaSearch.length === 1) return answer(viaSearch[0], "source:stated");
      // "LinkedIn Jobs" among "LinkedIn Post", "LinkedIn InMail" and alumni
      // groups "…Job Board or LinkedIn Group" (Samsara, question bank
      // 2026-10-05): the channel's own jobs, never a group, post or message.
      const theirJobs = opts.filter(
        (o) => offered.test(qn(o)) && keep(o) && /\bjobs?\b/.test(qn(o)) && !/\b(alumni|group|groups|community|inmail|message|post|posts|reach out|fair|event)\b/.test(qn(o))
      );
      if (theirJobs.length === 1) return answer(theirJobs[0], "source:stated");
    }
    // A stated channel the list does not offer. A job site is still a job
    // board ("LinkedIn" -> "Online Job Board", below); anything else is
    // "Other", never the default job board ("Career fair" got "Online Job
    // Board" on Paylocity, live 2026-10-03).
    if (!/\b(linked ?in|indeed|glassdoor|monster|zip ?recruiter|job (board|site|search)s?|online)\b/.test(stated)) {
      const other = opts.filter((o) => /^other\b/.test(qn(o)));
      return other.length === 1 ? answer(other[0], "source:stated-other") : null;
    }
    // A job site stated and none of the list's matching ones unique: only a
    // job-board option may take it, never the company's own site ("Job
    // board" got "Affirm’s Career Site", question bank 2026-10-05). Several
    // named boards and no general one: the applicant picks which.
    const general = opts.filter((o) => !/\b(university|campus|school|college|internal)\b/.test(qn(o)));
    // A generic online option, never a search engine's brand: "Job board"
    // became "Google Search" (Commvault, regression 2026-10-05).
    // Nor the company's own channels ("Coveo Blog or Website Content").
    const ours = (o: string): boolean => Boolean(company.trim()) && ` ${qn(o)} `.includes(` ${qn(company)} `);
    // "Internet / Online", "Online job ad": not "Online community" (Workleap),
    // "Online Forum or Community" (Coveo) or a "Search Engine" (Accenture
    // Federal; question bank 2): a job board is none of those.
    const ONLINE = /^((the|an?|internet|online|web)\s*)+$|\b(internet|online|web) (job|jobs|ad|ads|advert|advertisement|posting|listing)s?\b|\bthird party (website|site|job site|job board)s?\b/;
    // A named platform stated is that platform: "LinkedIn" is never another
    // brand's board ("Handshake", Geotab; question bank 2026-10-05).
    const brands = NAMED_SOURCE.test(stated) ? [SOURCE_PREFERENCE[0], ONLINE] : [SOURCE_PREFERENCE[0], ONLINE, SOURCE_PREFERENCE[4]];
    for (const re of brands) {
      const hits = general.filter((o) => re.test(qn(o)) && !ours(o) && !outreach(o) && !otherBoard(o) && !(NAMED_SOURCE.test(stated) && NAMED_SOURCE.test(qn(o))));
      if (hits.length === 1) return answer(hits[0], "source:stated-board");
      const unbranded = hits.filter((o) => !NAMED_SOURCE.test(qn(o)));
      if (hits.length > 1 && unbranded.length === 1) return answer(unbranded[0], "source:stated-board");
    }
    // A platform named ("LinkedIn") that the list neither offers nor holds a
    // general board for: "Other" is what is true (Nutanix's "Other - please
    // list source below"). A generic "job board" over named boards alone
    // stays the applicant's: which one it was, nobody said.
    const named = brandOf(stated);
    if (named && !opts.some((o) => brandOf(o) === named)) {
      const other = opts.filter((o) => /^other\b/.test(qn(o)));
      if (other.length === 1) return answer(other[0], "source:stated-other");
    }
    // A list with no online channel at all (Figma: FigFest | a partnership |
    // an on-campus event | a virtual event | Other): the job site is "Other".
    const other = opts.filter((o) => /^other\b/.test(qn(o)));
    if (other.length === 1 && !opts.some((o) => o !== other[0] && /\b(job|jobs|board|search|posting|online|website|site|internet|web|career|careers|social|linked ?in|indeed|glassdoor)\b/.test(qn(o)))) {
      return answer(other[0], "source:stated-other");
    }
    return null;
  }
  // No stated channel: never a campus one, even as the only careers-site
  // option ("Campus Career Site", Enova on Greenhouse, a real profile
  // 2026-10-03), and among several job searches the one no brand names
  // ("Other - Job Site" beside "LinkedIn Job Search", Planet: left blank).
  const general = opts.filter((o) => !/\b(university|campus|school|college|internal)\b/.test(qn(o)));
  for (const re of SOURCE_PREFERENCE) {
    const hits = general.filter((o) => re.test(qn(o)));
    if (hits.length === 1) return answer(hits[0], "source:default");
    const unbranded = hits.filter((o) => !NAMED_SOURCE.test(qn(o)));
    if (hits.length > 1 && unbranded.length === 1) return answer(unbranded[0], "source:default");
  }
  return null;
}

/**
 * A requirement acknowledged through location options ("Yes, I'm currently
 * located here" | "Yes, I'd relocate prior to the start of the role" | "No,
 * I'm not located nearby", Brex 2026-10-03): located here when the applicant
 * lives in the job's city, else relocate unless they said they would not.
 */
function chooseLocated(q: QuestionInput, profile: UserApplicationProfile, facts: ProfileFacts, ctx: QuestionContext): QuestionResult {
  const opts = realOptions(q);
  // "No, I'm not located nearby" also says "located nearby": polarity first.
  const here = opts.filter(
    (o) => optionPolarity(o) !== false && /\b(currently (located|live|living|based|reside|residing)|located (here|nearby|in the area|locally)|live (here|nearby|locally|in the area)|already (live|located|based))\b/.test(qn(o))
  );
  const move = opts.filter((o) => /\b(relocat\w*|move|moving)\b/.test(qn(o)) && optionPolarity(o) !== false);
  const no = opts.filter((o) => optionPolarity(o) === false);
  if (here.length !== 1 || move.length !== 1) return null;
  const city = isHigh(facts.location.city) ? qn(facts.location.city.value) : "";
  const local = Boolean(city && ctx.jobCity && qn(ctx.jobCity) === city);
  if (local) return answer(here[0], "located:here");
  const refuses = /^no\b/i.test((profile.willingToRelocate ?? "").trim());
  if (refuses) return no.length === 1 ? answer(no[0], "located:not-relocating") : null;
  return answer(move[0], profile.willingToRelocate ? "located:relocate-stated" : "located:relocate-default");
}

/** The sentence a label ends by asking: "This role requires full-time, onsite
 *  work (Monday–Friday). Which location can you reliably commute to?" asks
 *  "which location…", whatever the requirement before it. */
function askedSentence(label: string): string {
  const parts = sentencesOf(label);
  return qn([...parts].reverse().find((s) => /\?\W*$/.test(s)) ?? parts[parts.length - 1] ?? "");
}

/**
 * "Which location can you reliably commute to?" with the posting's offices as
 * options and a way to say none ("Lincoln, RI" | "Orlando, FL" | "Neither
 * location", FSSI on Workable, live 2026-10-03): the applicant's own city, or
 * the none option for someone far from every office who will not move. A
 * mover chooses for themselves.
 */
function choosePlaceOption(q: QuestionInput, profile: UserApplicationProfile, facts: ProfileFacts): QuestionResult {
  const opts = realOptions(q);
  if (opts.length < 2) return null;
  const none = opts.filter((o) => /^(neither|none|no)\b|\b(neither|none of|not able|unable|cannot|can ?t)\b/.test(qn(o)));
  const places = opts.filter((o) => !none.includes(o)).map((o) => ({ o, p: placeOf(o) }));
  if (places.length === 0 || places.some(({ p }) => !p.city || !(p.region || p.country))) return null;
  const homeCity = isHigh(facts.location.city) ? qn(facts.location.city.value) : null;
  const local = places.filter(({ p }) => homeCity && p.city === homeCity);
  if (local.length === 1) return answer(local[0].o, "default:commute-local");
  if (local.length === 0 && none.length === 1 && /^no\b/i.test((profile.willingToRelocate ?? "").trim())) {
    return answer(none[0], "default:commute-none");
  }
  return null;
}

/**
 * The default answer for `q`, or null when no family applies. `n` is the
 * normalized label (label + help text when the label is a bare stem).
 */
export function resolveDefault(
  q: QuestionInput,
  n: string,
  profile: UserApplicationProfile,
  facts: ProfileFacts,
  ctx: QuestionContext
): QuestionResult {
  const kind = q.kind;
  const choiceLike =
    kind === "boolean" || kind === "choice" || q.controlType === "checkbox" || realOptions(q).length > 0 ||
    q.controlType === "combobox" || q.controlType === "customDropdown";

  // A follow-up STARTING with "if" ("If you heard about us through a referral,
  // please state…") is not the channel question; an "If referred, by who?"
  // add-on after it is (Kenect on Breezy, live 2026-10-03).
  if (HOW_HEARD.test(n) && !/^\s*if\b/.test(n)) return chooseSource(q, profile, ctx.company);
  const opts = realOptions(q);
  if ((UNLABELED.test(n) || /\b(find|found|hear|heard|learn|learned|discover)\w* (us|about us|this (role|job|position))\b/.test(n)) && opts.filter((o) => CHANNEL.test(qn(o))).length >= 3) {
    return chooseSource(q, profile, ctx.company);
  }
  // Who referred a referred applicant is theirs to write: their own name was
  // typed for "If you were referred by a current employee, what is the
  // employee's full name?" (Renaissance; question bank 2026-10-05).
  // ("the name you would like to be referred to as" is no referral, Asana.)
  // "Were you referred to Artera? If so, please provide their first and last
  // name." (Lever, question bank 2026-10-08) got the applicant's own name.
  if (!choiceLike && /\breferred (by|you|for)\b|\breferral\b|\breferrer\b|\bwho referred\b|\brefer(red)? you\b|\bwere you referred\b/.test(n) && /\breferr|\bemployee\b|\bfriend\b/.test(qn(profile.howDidYouHear ?? ""))) return REFERRER_UNKNOWN;
  if (!choiceLike) return unencumberedText(q, n, profile);
  // Agreeing to arbitrate is the applicant's alone, never ours or the AI's:
  // Roblox's, Asana's and Anthropic's agreements were accepted while Block's
  // and sweetgreen's were left blank (question bank 2026-10-08).
  if (WAIVES_RIGHTS.test(`${n} ${qn(opts.join(" "))}`)) return RIGHTS_THEIRS;
  // A long statement is judged by what it asks: SSCI's certification mentions
  // "a consumer credit report or criminal records check" and asks nothing
  // about a record (Workable bank, 2026-10-05: left unaccepted).
  const asked = qn(askedOfStatement(q.label || "", n.split(" ").length));
  if (CRIMINAL.test(asked)) return null;
  // "Preferred Contact Method" (Email | Mobile Phone | Home Phone; Dayforce,
  // live 2026-10-05): the email the application gives. A list without one is
  // the applicant's to choose.
  if (CONTACT_METHOD.test(n)) {
    const email = opts.filter((o) => /\be ?mail\b/.test(qn(o)));
    return email.length === 1 ? answer(email[0], "default:contact-by-email") : null;
  }
  // "Will you need an accommodation for your interview?" (Netlify, left blank
  // 2026-10-03): No for an applicant who stated no disability. Anyone else
  // answers it themselves. Read on the device; only the No leaves it.
  // Options that only consent or refuse make an acknowledgement, not a
  // question about the applicant (JazzHR's ended with an ADA sentence, and its
  // "accommodation" read as this rule: "I do not Consent", live 2026-10-05).
  const consentOnly = opts.length > 0 && opts.every(isConsentOption);
  // At work too: "Do you require workplace accommodations due to a mental
  // health condition? (Whether work from home or in-office)" (Wattpad on
  // Lever, question bank 2026-10-08) read as an in-office requirement, and
  // everyone willing to move said Yes. Anyone who has not stated "no
  // disability" answers it themselves, and the AI never does.
  // Asked, not mentioned: a statement's "I agree to request a reasonable
  // accommodation … if one is necessary" (National Journal's terms) asks nothing.
  if (!consentOnly && /\b(do|will|would|may|are) you\b[^?]{0,80}\b(need|require|request)\w*\b[^?]{0,80}\baccommodations?\b/.test(n) && /\b(interview|hiring|application|recruit\w*|workplace|disabilit\w*|medical|health|condition|impairment)\b/.test(n) && !/\b(housing|lodging|hotel|apartment)\b/.test(n)) {
    return /^no\b|\bdo not have\b|\bdont have\b/.test(qn(profile.eeo?.disabilityStatus ?? "")) ? polar(false, q, "default:no-accommodation") : ACCOMMODATION_THEIRS;
  }
  // A required list whose ONLY option is an acknowledgement ("I will read the
  // arbitration agreement below.", Anthropic; "Summer 2027" under "Please
  // confirm the season…", Astranis; live 2026-10-03): there is nothing else
  // to answer.
  // So is a first-person statement over a lone "Yes" ("I understand that
  // Coinbase may use AI tools…", question bank 2026-10-05).
  // Being recorded stays the applicant's choice here too (Sweetgreen's "the
  // video recording itself", question bank 2026-10-05); a rule against the
  // candidate recording (Block's "Recording any part of the interview without
  // consent is prohibited") is no such consent.
  if (opts.length === 1 && (ACK_OPTION.test(qn(opts[0])) || /\b(confirm|acknowledge|please read)\b/.test(n) || (/^(i|we) (understand|acknowledge|agree|certify|confirm|accept|have read)\b/.test(n) && /^yes$/i.test(opts[0].trim()))) && !MARKETING.test(qn(opts[0])) && !APPLICANT_RECORDED.test(n)) {
    return answer(opts[0], "default:only-option");
  }
  // SMS consent asked by its options under a field-name label ("Phone": "Yes -
  // I consent to receiving text messages", Ramp on Ashby).
  if (n.split(" ").length <= 3 && opts.length > 0 && MARKETING.test(qn(opts.join(" "))) && /\b(consent|opt|receive|receiving|subscribe)\b/.test(qn(opts.join(" ")))) {
    return polar(false, q, "default:marketing-opt-out");
  }

  // "Join the talent community and sign up for job alerts" (Waymo, live
  // 2026-10-05) is a subscription, whatever else it keeps on file.
  // Experience in marketing is no opt-in: "Do you have experience with Direct
  // Marketing Campaigns?" was No for everyone (Data Lab on Lever, question
  // bank 2026-10-08).
  if (MARKETING.test(n) && !EXPERIENCE_ASKED.test(n) && (!FUTURE_ROLES.test(n) || /\b(sign up|subscribe|job alerts?|newsletters?|mailing list)\b/.test(n))) {
    return polar(false, q, "default:marketing-opt-out");
  }
  if (FUTURE_ROLES.test(n) && /\b(consider|keep|retain|share|contact|notify|would you like|interested)\b/.test(n)) {
    return polar(true, q, "default:future-roles");
  }

  if (PRIOR_APPLICATION.test(n)) return polar(false, q, "default:no-prior-application");
  // Who completed the application is never ours to pledge (decision 6 of
  // round 4): "…to ensure each application is completed by a person. Please
  // select 'I confirm I am completing this application myself'" (Kobie on
  // Lever, live 2026-10-08) went to the AI.
  if (AUTHORSHIP.test(`${n} ${qn(opts.join(" "))}`)) return AUTHORSHIP_THEIRS;
  // "…Are you aware this is not a permanent role, and are you still
  // interested in being considered?" (Glossier, question bank 2026-10-05):
  // applying says so.
  if (/\bare you (still )?interested in (being considered|this (role|position|opportunity|job))\b/.test(n)) return polar(true, q, "default:still-interested");
  // "Will you be serving as enlisted personnel in either the Reserves or the
  // National Guard while working for AFS?" (live 2026-10-05): No for someone
  // who never served; anyone who did answers it.
  if (/\b(reserves?|national guard|reservist|enlisted|active duty)\b/.test(n) && /\b(will|are|do) you\b/.test(n)) {
    return /\bnever served\b/.test(qn(profile.eeo?.veteranStatus ?? "")) ? polar(false, q, "default:never-served") : null;
  }
  // "At your current employer, are you currently working on a project with
  // Accenture…?" for an applicant with no job now: No.
  if (/\bat your current (employer|company|job)\b/.test(n) && facts.employment.currentlyEmployed?.value === false) {
    return polar(false, q, "default:no-current-employer");
  }
  // "Have you previously worked for this organization" (Commvault): when the
  // company's name is not on the page to check against the profile.
  if (/\b(previously|ever|formerly|before) (worked|been employed) (for|at|with|by) (us|this (company|organi[sz]ation|employer|firm)|our (company|organi[sz]ation))\b/.test(n)) {
    return polar(false, q, "default:not-former-employee");
  }
  if (REFERRED.test(n)) {
    // Someone who said they were referred was ("Referral" chose "Employee
    // Referral" in a list, then No here; question bank 2026-10-05). A friend
    // need not be an employee: theirs to say.
    const said = qn(profile.howDidYouHear ?? "");
    const referred = /\breferr|\bemployee\b|\bfriend\b/.test(said);
    // Who referred them is theirs to write: "If you were referred by a current
    // employee, what is the employee's full name?" got the applicant's own
    // name once the referral was known (Renaissance; question bank 2026-10-05).
    if (referred && realOptions(q).length === 0 && q.controlType !== "checkbox" && q.kind !== "boolean") return REFERRER_UNKNOWN;
    if (/\breferr|\bemployee\b/.test(said)) return polar(true, q, "default:referred-stated");
    if (referred) return null;
    return polar(false, q, "default:not-referred");
  }
  if (RELATIVES.test(n) && /\b(work|employ|empl|staff|board|director|officer|relationship)\w*/.test(n)) {
    return polar(false, q, "default:no-relatives-inside");
  }
  if (CONFLICTS.test(n)) return polar(false, q, "default:no-conflict");
  if (NON_COMPETE.test(n)) return AFFIRMS_NONE.test(n) ? null : polar(false, q, "default:no-non-compete");
  const notGov = notGovernment(n, profile);
  if (notGov !== null) return notGov ? polar(false, q, "default:not-government-official") : GOVERNMENT_HISTORY;
  // "Are you currently conducting research related to…", "Do you currently
  // receive any active funding (grants, sponsorships)?" (Zoox on Lever, live
  // 2026-10-03): No for someone out of school; a student's lab or grant is theirs.
  if (RESEARCH_OR_FUNDING.test(n)) {
    const entries = facts.education.entries;
    return entries.length > 0 && entries.every((e) => e.completed === true) ? polar(false, q, "default:no-research-or-funding") : null;
  }

  // Consent-only options, after the opt-outs above: the acknowledgement is
  // given whatever its paragraph mentions (an EEO statement, the ADA).
  if (consentOnly && !RECORDING.test(n)) return polar(true, q, "default:consent");
  if (DEMOGRAPHIC.test(n)) return null;
  // Being recorded or transcribed (AI notetakers, Palantir on Lever, live
  // 2026-10-03) is the applicant's own choice: never defaulted, nor the AI's.
  // Named by the options alone too: "Candidate Consent Acknowledgment" over
  // "I agree and consent to the recording and AI summarization of my
  // interview." (TensorWave on Ashby, live 2026-10-08) went to the AI.
  const recordingOption = opts.some((o) => RECORDING.test(qn(o)) && /\b(consent|agree|opt (in|out))\b/i.test(o));
  if ((RECORDING.test(n) && (/\b(consent|opt (in|out)|agree)\b/.test(n) || opts.some((o) => /\bconsent\b/i.test(o)))) || recordingOption) {
    return RECORDING_CONSENT;
  }
  if (CONSENT_VERB.test(n) && CONSENT_OBJECT.test(n) && !RECORDING.test(n)) {
    return polar(true, q, "default:consent");
  }
  // A titled policy or agreement with a bare Yes / No and no question in it.
  if (!/\?/.test(q.label) && TITLED_TERMS.test(n) && !RECORDING.test(n) && n.split(" ").length <= 8 && opts.length === 2 && opts.every((o) => /^(yes|no)$/i.test(o.trim()))) {
    return polar(true, q, "default:consent");
  }
  if (/\bessential (functions|duties)\b/.test(n)) return polar(true, q, "default:essential-functions");
  // Working in person somewhere named, for an applicant who will not relocate:
  // "Are you able to work a Hybrid schedule out of Pleasant Grove, Utah?" got
  // Yes for an Austin applicant (Kenect on Breezy, live 2026-10-03). Their own
  // city: Yes; another state or country: No; another city at home: theirs.
  // A question naming no full place ("in one of our offices", "at Mindex's
  // Rochester office") means the posting's own places: Anthropic's San
  // Francisco | New York City | Washington, DC got Yes for an Austin applicant,
  // and so did Mindex's Rochester, New York (live 2026-10-03).
  // Occasional presence is not in-person work: "attend team gatherings a few
  // times a year" is travel, which someone who will not move can still do.
  // A statement of awareness over Yes / No is acknowledged: "I am aware that
  // this is a hybrid role, with 3 days in office expectation, based out of
  // Foster City, CA." (Zoox on Lever, question bank 2026-10-08) was read as
  // an in-office question, and No for anyone who will not move.
  // A promise inside it is no awareness: "I understand that this role is in
  // either Phoenix, AZ or Atlanta, GA and I am willing to participate in a
  // hybrid…" (Samsara) is the in-person question below.
  if (/^(i am|i m|im) (now )?aware\b|^i (understand|acknowledge|recognize)\b/.test(n) && !/\b(i|i m|i am) (also )?(willing|able|available|commit\w*|agree|plan|intend)\b|\bi (can|will|would)\b/.test(n) && !/\?/.test(q.label) && opts.length === 2 && opts.every((o) => /^(yes|no)$/i.test(o.trim()))) {
    return polar(true, q, "default:aware");
  }
  const occasional = OCCASIONAL_PRESENCE.test(n) && !REGULAR_PRESENCE.test(n);
  // A requirement followed by a question asking WHICH ("…onsite work. Which
  // location can you reliably commute to?", FSSI on Workable, live
  // 2026-10-03) is never a yes or no: before its options loaded it was
  // proposed "Yes". Only an option a stated fact picks.
  if ((IN_PERSON.test(n) || REQUIREMENT.test(n)) && /^(which|what|where|when|how|who)\b/.test(askedSentence(q.label))) {
    return choosePlaceOption(q, profile, facts);
  }
  if (!occasional) {
    const onsite = onsiteVerdict(q.label, profile, facts, ctx);
    if (onsite === "local") return polar(true, q, "default:in-person-local");
    if (onsite === "elsewhere") return polar(false, q, "default:in-person-not-relocating");
    if (onsite === "unclear") return null;
  }
  // A request is one ASKED: "relocation assistance is not provided. Can you
  // meet this requirement?" (Breezy, live 2026-10-05) asks the requirement.
  // Pay is the applicant's anywhere in the question ("…aligned with the
  // compensation package above… Does this align with your pay
  // expectations?", Nuro).
  // Meeting a location requirement NOW is where one lives, not a willingness
  // to move: "Do you currently meet the worksite location requirements for
  // being On-site or Hybrid…?" got Yes from Toronto for a job in Korea
  // (Credence on Workable, 2026-10-05). Another country: No.
  if (/\bcurrently (meet|satisfy|fulfil+)\b[^?]{0,40}\b(location|worksite|work site|on ?site|in ?office|commut\w*)\b/.test(n)) {
    const home = isHigh(facts.location.country) ? facts.location.country.value.code : null;
    return home && ctx.jobCountry && home !== ctx.jobCountry ? polar(false, q, "default:not-there-now") : null;
  }
  // Experience is the applicant's history, no requirement to accept: "Do you
  // have experience managing program risks… cost, schedule, and performance
  // objectives?" was Yes for everyone by its "schedule" (Saalex on Workable,
  // 2026-10-05).
  if (
    REQUIREMENT.test(n) &&
    !ASSISTANCE.test(askedSentence(q.label)) &&
    !PAY_TERMS.test(n) &&
    !EXPERIENCE_ASKED.test(n) &&
    (ACK_VERB.test(n) || /^(do|are|will|can|would) you\b/.test(n))
  ) {
    // "Do you have any impediments to traveling internationally?" (Veeva on
    // Lever, live 2026-10-03, answered Yes): an obstacle question's clean answer is No.
    // An obstacle ASKED about ("any impediments", "anticipate challenges"), not
    // one ruled out in the question ("travel with no restrictions").
    if (OBSTACLE.test(n) && /\b(any|anticipate|foresee|are there)\b/.test(n)) return polar(false, q, "default:no-obstacle");
    // Options that answer through location ("currently located here" vs
    // "I'd relocate") need the choice, not a bare yes.
    const located = chooseLocated(q, profile, facts, ctx);
    if (located) return located;
    const refusesMove = /\brelocat|\b(move|moving) to\b/.test(n) && /^no\b/i.test((profile.willingToRelocate ?? "").trim());
    if (refusesMove) return polar(false, q, "relocation:stated");
    // In person where nobody said: "This position is onsite in the studio
    // location listed in the posting. Are you willing to work onsite in that
    // location?" (Larian on Lever, question bank 2026-10-08, its place not on
    // the page) was Yes from Seattle for someone who will not move. Places
    // named or posted were judged above; with none, it is theirs.
    if (!occasional && /\b(in ?office|on ?site|in ?person|hybrid)\b/.test(n) && /^no\b/i.test((profile.willingToRelocate ?? "").trim())) return null;
    // Travel now and then is no office work: "…employees may periodically
    // travel… Are you able and willing to travel as needed?" (Kobie on Lever,
    // live 2026-10-08) was left to someone who prefers remote work.
    const remoteOnly = !occasional && /\b(in ?office|on ?site|in ?person|hybrid)\b/.test(n) && /^remote$/i.test((profile.workPreference ?? "").trim());
    if (remoteOnly) return null; // they said remote: the applicant must decide
    return polar(true, q, "default:accepts-requirement");
  }
  return null;
}

