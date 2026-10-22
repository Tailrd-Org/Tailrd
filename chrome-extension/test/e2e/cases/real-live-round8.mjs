/**
 * Round 8 (2026-10-21): SmartRecruiters to the screening step, more
 * Greenhouse, Ashby and Lever boards, the smaller ATSs. Same posture as every
 * live case: GET and GraphQL queries only, never a submit, no account.
 */
import {
  BERLIN_STAFF,
  BOOTCAMP_CAREER_GAP,
  COMPLETE_CANADIAN,
  INDIA_NEW_GRAD,
  MONTREAL_CHANGER,
  NEW_GRAD_DECLINER,
  SPARSE_CANADIAN,
  UK_SENIOR,
  US_F1_STUDENT,
  US_GREENCARD_STUDENT,
  US_H1B_SENIOR,
  US_OPT_ANALYST,
  US_VETERAN,
} from "../profiles.mjs";

const live = (c) => ({ mode: "live", trigger: { fillTimeoutMs: 180000 }, expect: {}, ...c });
const sr = (company, uuid) => `https://jobs.smartrecruiters.com/oneclick-ui/company/${company}/publication/${uuid}?dcr_ci=${company}`;
const gh = (board, id) => `https://job-boards.greenhouse.io/${board}/jobs/${id}`;
const ashby = (board, id) => `https://jobs.ashbyhq.com/${board}/${id}/application`;
const lever = (board, id) => `https://jobs.lever.co/${board}/${id}/apply`;

/** Comeet's job page opens its form in a frame of its own when "Apply for
 *  this job" is clicked: the user's click, then the frame. */
async function userOpensComeetForm(page) {
  const apply = page.locator('button:has-text("Apply for this job")').first();
  await apply.waitFor({ state: "attached", timeout: 15000 });
  await apply.evaluate((el) => el.click());
  await page.waitForSelector('iframe[src*="/apply?"]', { state: "attached", timeout: 15000 });
}

export default [
  // ------------------------- batch A: SmartRecruiters, to the screening step
  // What held round 7's postings on page 1 is the posting's own required
  // résumé ("Please attach your resume to complete this application"): the
  // one-click context says so (`features.resumeRequired`), and a run uploads
  // nothing. These 13 prod postings (2026-10-21) take the résumé as optional,
  // so Next opens their screening step with no upload (EVERSANA's and
  // LinkedIn's require experience instead). Each runs in a browser of its own.
  live({
    id: "r8a-sr-servicenow-assoc",
    ats: "smartrecruiters",
    profile: US_F1_STUDENT,
    url: sr("ServiceNow", "749de8bb-34ce-4a49-b2d8-48c535bc9956"), nextPages: 1,
    expect: {
      "label:First name": "Arjun",
      "label:Last name": "Mehta",
      "label=Email*": "arjun.mehta@example.com",
      "label:Confirm your email": "arjun.mehta@example.com",
      "label:City": "Cambridge, MA, US",
      "label:Phone number": "617 555 0199",
      "label:LinkedIn": "https://linkedin.com/in/arjunmehta",
    },
  }),
  live({
    id: "r8a-sr-servicenow-swe",
    ats: "smartrecruiters",
    profile: US_GREENCARD_STUDENT,
    url: sr("ServiceNow", "611bfd47-bd06-4eb3-99e3-e307dff74ce5"), nextPages: 1,
    expect: {
      "label:First name": "Mei",
      "label:Last name": "Lin",
      "label=Email*": "mei.lin@example.edu",
      "label:Confirm your email": "mei.lin@example.edu",
      "label:City": "San Jose, CA, US",
      "label:Phone number": "408 555 0172",
      "label:LinkedIn": "https://www.linkedin.com/in/meilin",
      "label:Website": "https://meilin.github.io",
    },
  }),
  live({
    id: "r8a-sr-servicenow-fdrp",
    ats: "smartrecruiters",
    profile: UK_SENIOR,
    url: sr("ServiceNow", "6efc8740-a1e7-4f73-859b-4736906924d9"), nextPages: 1,
    expect: {
      "label:First name": "Siobhán",
      "label:Last name": "O'Neill",
      "label=Email*": "siobhan.oneill@example.co.uk",
      "label:Confirm your email": "siobhan.oneill@example.co.uk",
      "label:City": "London, England, United Kingdom",
      "label:Country code": "+ 44",
      "label:Phone number": "20 7946 0958",
      "label:LinkedIn": "https://www.linkedin.com/in/siobhan-oneill",
      "label:Website": "https://soneill.dev",
    },
  }),
  live({
    id: "r8a-sr-servicenow-finance",
    ats: "smartrecruiters",
    profile: NEW_GRAD_DECLINER,
    url: sr("ServiceNow", "b3ba4b29-9112-4183-b619-aa51ac29f67e"), nextPages: 1,
    expect: {
      "label:First name": "Priya",
      "label:Last name": "Nair",
      "label=Email*": "priya.nair@example.com",
      "label:Confirm your email": "priya.nair@example.com",
      "label:City": "Calgary, Alberta, Canada",
      "label:Country code": "+ 1",
      "label:Phone number": "403 555 0123",
    },
  }),
  live({
    id: "r8a-sr-intuitive-fs1",
    ats: "smartrecruiters",
    profile: US_H1B_SENIOR,
    url: sr("Intuitive", "07f9a52b-ba30-4629-abc9-a64c18f4ae43"), nextPages: 1,
    expect: {
      "label:First name": "Thu Hà",
      "label:Last name": "Nguyễn",
      "label=Email*": "thuha.nguyen@example.com",
      "label:Confirm your email": "thuha.nguyen@example.com",
      "label:City": "Seattle, WA, US",
      "label:Phone number": "206 555 0131",
      "label:LinkedIn": "https://www.linkedin.com/in/thuha-nguyen",
    },
  }),
  live({
    id: "r8a-sr-intuitive-fs2",
    ats: "smartrecruiters",
    profile: US_VETERAN,
    url: sr("Intuitive", "32a72cca-295c-4266-b854-dd0240ba1146"), nextPages: 1,
    expect: {
      "label:First name": "Marcus",
      "label:Last name": "Hill",
      "label=Email*": "marcus.hill@example.com",
      "label:Confirm your email": "marcus.hill@example.com",
      "label:City": "Austin, TX, US",
      "label:Country code": "+ 1",
      "label:Phone number": "512 555 0143",
      "label:LinkedIn": "https://www.linkedin.com/in/marcus-hill-dev",
    },
  }),
  live({
    id: "r8a-sr-bosch-facility",
    ats: "smartrecruiters",
    profile: BOOTCAMP_CAREER_GAP,
    url: sr("BoschGroup", "7b3825cb-cb81-4256-9b8c-581fe371c392"), nextPages: 1,
    expect: {
      "label:First name": "D'Andre",
      "label:Last name": "Jean-Baptiste",
      "label=Email*": "dandre.jb@example.com",
      "label:Confirm your email": "dandre.jb@example.com",
      "label:City": "Denver, CO, US",
      "label:Phone number": "720 555 0164",
      "label:LinkedIn": "https://linkedin.com/in/dandre-jean-baptiste/",
      "label:Website": "https://dandre.codes",
    },
  }),
  live({
    id: "r8a-sr-bosch-hr",
    ats: "smartrecruiters",
    profile: COMPLETE_CANADIAN,
    url: sr("BoschGroup", "6ba6cf3a-ecce-44d2-9681-7d21f4dac817"), nextPages: 1,
    expect: {
      "label:First name": "Maya",
      "label:Last name": "Tremblay",
      "label=Email*": "maya.tremblay@example.com",
      "label:Confirm your email": "maya.tremblay@example.com",
      "label:City": "Toronto, Ontario, Canada",
      "label:Country code": "+ 1",
      "label:Phone number": "416 555 0142",
      "label:LinkedIn": "https://www.linkedin.com/in/maya-tremblay",
      "label:Website": "https://mayatremblay.dev",
    },
  }),
  live({
    id: "r8a-sr-bosch-calibration",
    ats: "smartrecruiters",
    profile: INDIA_NEW_GRAD,
    url: sr("BoschGroup", "3913d946-2d13-4ea4-8a56-b36ac5a3c585"), nextPages: 1,
    expect: {
      "label:First name": "Ananya",
      "label:Last name": "Raghavan",
      "label=Email*": "ananya.raghavan@example.in",
      "label:Confirm your email": "ananya.raghavan@example.in",
      "label:City": "Bengaluru, Karnataka, India",
      "label:Country code": "+ 91",
      "label:Phone number": "98450 12345",
      "label:LinkedIn": "https://www.linkedin.com/in/ananya-raghavan",
    },
  }),
  live({
    id: "r8a-sr-bostoneng",
    ats: "smartrecruiters",
    profile: US_OPT_ANALYST,
    url: sr("BostonEngineeringCorporation", "d4123676-8efd-4b15-9c06-d08e248b1e9d"), nextPages: 1,
    expect: {
      "label:First name": "Ji-woo",
      "label:Last name": "Park",
      "label=Email*": "jiwoo.park@example.com",
      "label:Confirm your email": "jiwoo.park@example.com",
      "label:City": "Boston, MA, US",
      "label:Phone number": "617 555 0148",
      "label:LinkedIn": "https://www.linkedin.com/in/jiwoo-park-data",
      "label:Website": "https://jiwoopark.io",
    },
  }),
  live({
    id: "r8a-sr-circle",
    ats: "smartrecruiters",
    profile: MONTREAL_CHANGER,
    url: sr("CircleLogistics1", "d20c0deb-3cf7-44d2-b521-9aa234660fa7"), nextPages: 1,
    expect: {
      "label:First name": "Alex",
      "label:Last name": "Côté-Tremblay",
      "label=Email*": "alex.cote@example.ca",
      "label:Confirm your email": "alex.cote@example.ca",
      "label:City": "Montreal, Quebec, Canada",
      "label:Country code": "+ 1",
      "label:Phone number": "514 555 0186",
      "label:LinkedIn": "linkedin.com/in/alexcote",
    },
  }),
  live({
    id: "r8a-sr-eversana",
    ats: "smartrecruiters",
    profile: BERLIN_STAFF,
    url: sr("EVERSANA1", "c72e43d8-e37d-4233-bb39-f2b394d6aea4"), nextPages: 1,
    expect: {
      "label:First name": "Jürgen",
      "label:Last name": "Weiß",
      "label=Email*": "juergen.weiss@example.de",
      "label:Confirm your email": "juergen.weiss@example.de",
      "label:City": "Berlin, State of Berlin, Germany",
      "label:Country code": "+ 49",
      "label:Phone number": "30 12345678",
      "label:LinkedIn": "https://www.linkedin.com/in/juergen-weiss",
    },
  }),
  live({
    id: "r8a-sr-linkedin",
    ats: "smartrecruiters",
    profile: SPARSE_CANADIAN,
    url: sr("LinkedIn3", "001359e4-c46c-45cb-a388-bc79014089ec"), nextPages: 1,
    expect: {
      "label:First name": "Maya",
      "label:Last name": "Tremblay",
      "label=Email*": "maya.tremblay@example.com",
      "label:Confirm your email": "maya.tremblay@example.com",
      "label:City": "Toronto, Ontario, Canada",
      "label:Country code": "+ 1",
      "label:Phone number": "416 555 0142",
      "label:LinkedIn": "https://www.linkedin.com/in/maya-tremblay",
    },
  }),
  // The context's `features` are the company's defaults, not the posting's:
  // its form's own configuration (the page's GET of `.../config`) required a
  // résumé on Bosch's three and Circle's, and experience on EVERSANA's and
  // LinkedIn's. Of prod's 26 SmartRecruiters companies only ServiceNow,
  // Intuitive and Western Digital take a posting with nothing to upload; four
  // more of ServiceNow's, with round 7's three, make thirteen screening steps.
  live({
    id: "r8a-sr-servicenow-finance2",
    ats: "smartrecruiters",
    profile: US_OPT_ANALYST,
    url: sr("ServiceNow", "99e904e8-1e5a-4096-beb0-5996c4cf5427"), nextPages: 1,
    expect: {
      "label:First name": "Ji-woo",
      "label:Last name": "Park",
      "label=Email*": "jiwoo.park@example.com",
      "label:Confirm your email": "jiwoo.park@example.com",
      "label:City": "Boston, MA, US",
      "label:Phone number": "617 555 0148",
      "label:LinkedIn": "https://www.linkedin.com/in/jiwoo-park-data",
      "label:Website": "https://jiwoopark.io",
    },
  }),
  live({
    id: "r8a-sr-servicenow-fdrp2",
    ats: "smartrecruiters",
    profile: INDIA_NEW_GRAD,
    url: sr("ServiceNow", "60e192cf-cbad-42ef-a2d1-b2fd6a780a3c"), nextPages: 1,
    expect: {
      "label:First name": "Ananya",
      "label:Last name": "Raghavan",
      "label=Email*": "ananya.raghavan@example.in",
      "label:Confirm your email": "ananya.raghavan@example.in",
      "label:City": "Bengaluru, Karnataka, India",
      "label:Country code": "+ 91",
      "label:Phone number": "98450 12345",
      "label:LinkedIn": "https://www.linkedin.com/in/ananya-raghavan",
    },
  }),
  live({
    id: "r8a-sr-servicenow-moveworks",
    ats: "smartrecruiters",
    profile: COMPLETE_CANADIAN,
    url: sr("ServiceNow", "e48bcb74-d711-4d5e-b486-a19a247237dd"), nextPages: 1,
    expect: {
      "label:First name": "Maya",
      "label:Last name": "Tremblay",
      "label=Email*": "maya.tremblay@example.com",
      "label:Confirm your email": "maya.tremblay@example.com",
      "label:City": "Toronto, Ontario, Canada",
      "label:Country code": "+ 1",
      "label:Phone number": "416 555 0142",
      "label:LinkedIn": "https://www.linkedin.com/in/maya-tremblay",
      "label:Website": "https://mayatremblay.dev",
    },
  }),
  live({
    id: "r8a-sr-servicenow-staff",
    ats: "smartrecruiters",
    profile: BERLIN_STAFF,
    url: sr("ServiceNow", "64dd40e1-94d0-4d8f-b6b5-acb2f9c85416"), nextPages: 1,
    expect: {
      "label:First name": "Jürgen",
      "label:Last name": "Weiß",
      "label=Email*": "juergen.weiss@example.de",
      "label:Confirm your email": "juergen.weiss@example.de",
      "label:City": "Berlin, State of Berlin, Germany",
      "label:Country code": "+ 49",
      "label:Phone number": "30 12345678",
      "label:LinkedIn": "https://www.linkedin.com/in/juergen-weiss",
    },
  }),

  // ----------------------------- batch B: Greenhouse boards in no bank and no case
  // From prod's active postings (2026-10-21): 27 greenhouse.io boards were in
  // no question bank and no case; the 13 asking the most (public job API,
  // GET), blind first.
  live({ id: "r8b-gh-insomniac", ats: "greenhouse", profile: COMPLETE_CANADIAN, url: gh("insomniac", "6227080004") }),
  live({ id: "r8b-gh-legend", ats: "greenhouse", profile: US_F1_STUDENT, url: gh("legendcareers", "4745118005") }),
  live({ id: "r8b-gh-horizon", ats: "greenhouse", profile: INDIA_NEW_GRAD, url: gh("horizonindustrieslimited", "5457606008") }),
  live({ id: "r8b-gh-clearstreet", ats: "greenhouse", profile: US_H1B_SENIOR, url: gh("clearstreet", "8304159") }),
  live({ id: "r8b-gh-allen", ats: "greenhouse", profile: US_OPT_ANALYST, url: gh("thealleninstitute", "8293615") }),
  live({ id: "r8b-gh-drivewealth", ats: "greenhouse", profile: NEW_GRAD_DECLINER, url: gh("drivewealth", "8038087003") }),
  live({ id: "r8b-gh-scopely", ats: "greenhouse", profile: BERLIN_STAFF, url: gh("scopely", "5456049008") }),
  live({ id: "r8b-gh-bracebridge", ats: "greenhouse", profile: US_GREENCARD_STUDENT, url: gh("bracebridgecapital", "4744999005") }),
  live({ id: "r8b-gh-evismart", ats: "greenhouse", profile: BOOTCAMP_CAREER_GAP, url: gh("evismart", "4457881009") }),
  live({ id: "r8b-gh-trace3", ats: "greenhouse", profile: US_VETERAN, url: gh("trace3", "8304310") }),
  live({ id: "r8b-gh-hearcom", ats: "greenhouse", profile: MONTREAL_CHANGER, url: gh("hearcom", "8290924") }),
  live({ id: "r8b-gh-via", ats: "greenhouse", profile: UK_SENIOR, url: gh("via", "8882311002") }),
  live({ id: "r8b-gh-i360", ats: "greenhouse", profile: SPARSE_CANADIAN, url: gh("i360technologiesinc", "4452476009") }),

  // ----------------------------------- batch C: Ashby boards in no bank and no case
  // 34 of prod's Ashby boards were in no bank and no case (2026-10-21); their
  // GET-and-GraphQL-query bank is results/qbank/ashby-r8.json. The 13 asking
  // the most, each on its prod posting, blind first.
  live({ id: "r8c-ashby-1password", ats: "ashby", profile: COMPLETE_CANADIAN, url: ashby("1password", "25a06bd3-2367-4cfc-8624-4c6d7c938459") }),
  live({ id: "r8c-ashby-amplify", ats: "ashby", profile: US_F1_STUDENT, url: ashby("amplify", "7b859cc1-1354-43c5-bbd4-0571e85f7c51") }),
  live({ id: "r8c-ashby-pebl", ats: "ashby", profile: US_GREENCARD_STUDENT, url: ashby("pebl", "49d8b355-620b-4dde-be4b-5489abf4c9cf") }),
  live({ id: "r8c-ashby-aurora", ats: "ashby", profile: US_H1B_SENIOR, url: ashby("aurora-operations-inc", "70b6130c-1207-47bc-8bba-06ad3a1d4154") }),
  live({ id: "r8c-ashby-handshake", ats: "ashby", profile: UK_SENIOR, url: ashby("handshake", "42aca7f5-2f58-40ef-a6ce-19027d32a12f") }),
  live({ id: "r8c-ashby-jane", ats: "ashby", profile: MONTREAL_CHANGER, url: ashby("jane", "6a644699-0d4d-4f4b-b471-7940a0bd74cc") }),
  live({ id: "r8c-ashby-illumio", ats: "ashby", profile: INDIA_NEW_GRAD, url: ashby("illumio", "09d4895b-6de4-4d22-bbc3-aa45f2b24260") }),
  live({ id: "r8c-ashby-patreon", ats: "ashby", profile: US_OPT_ANALYST, url: ashby("patreon", "71f644bc-0873-4d62-a599-bd64405d6587") }),
  live({ id: "r8c-ashby-agzen", ats: "ashby", profile: SPARSE_CANADIAN, url: ashby("agzen", "4cff4edd-3e9a-4ea1-8138-80c7241a2d3a") }),
  live({ id: "r8c-ashby-spruceid", ats: "ashby", profile: NEW_GRAD_DECLINER, url: ashby("spruceid", "3d182b7f-482e-45b2-8668-f04f0b5fda8e") }),
  live({ id: "r8c-ashby-metamorphic", ats: "ashby", profile: BOOTCAMP_CAREER_GAP, url: ashby("metamorphic", "cf518648-4e65-4f7d-aa05-f01066d8bd8a") }),
  live({ id: "r8c-ashby-commure", ats: "ashby", profile: US_VETERAN, url: ashby("commure", "259988fe-0389-461f-823c-36d81840d465") }),
  live({ id: "r8c-ashby-regent", ats: "ashby", profile: BERLIN_STAFF, url: ashby("regent", "698d38b7-c47e-40a1-bf78-31d59c395c23") }),

  // ------------------------------------------------------------ batch D: Lever
  // Prod's Lever boards in no bank and no case (2026-10-21): Humble Robotics
  // and Stand Together (new since 10-20), Significance and Deep Genomics.
  live({ id: "r8d-lever-humble", ats: "lever", profile: US_GREENCARD_STUDENT, url: lever("humble-robotics", "8d1fb5d7-9b07-4fac-8c39-e395dca7f2b3") }),
  live({ id: "r8d-lever-standtogether", ats: "lever", profile: US_OPT_ANALYST, url: lever("standtogether", "35deb481-fc0b-4513-abf5-68880006189b") }),
  live({ id: "r8d-lever-significance", ats: "lever", profile: US_VETERAN, url: lever("significanceinc", "0ae594c0-3af9-4f91-a521-ca5363822cd6") }),
  live({ id: "r8d-lever-deepgenomics", ats: "lever", profile: COMPLETE_CANADIAN, url: lever("deepgenomics", "2ec45717-17a1-47a7-8e41-737ec3bc9bf9") }),

  // ------------------------- batch E: Greenhouse's remix multi-selects (P1c)
  // Boards whose bank lists a multi_value_multi_select a profile answers:
  // languages (Riot, Jane Street), a nationality list (Canonical), the
  // undergraduate disciplines (DV Trading), how one heard (Gusto, Samsara).
  live({ id: "r8e-gh-riot", ats: "greenhouse", profile: MONTREAL_CHANGER, url: gh("riotgames", "8222014") }),
  live({ id: "r8e-gh-janestreet", ats: "greenhouse", profile: UK_SENIOR, url: gh("janestreet", "8599917002") }),
  live({ id: "r8e-gh-canonical", ats: "greenhouse", profile: UK_SENIOR, url: gh("canonical", "7661586") }),
  live({ id: "r8e-gh-dvtrading", ats: "greenhouse", profile: INDIA_NEW_GRAD, url: gh("dvtrading", "4732429005") }),
  live({ id: "r8e-gh-gusto", ats: "greenhouse", profile: US_OPT_ANALYST, url: gh("gusto", "8075901") }),
  // The same race list for a race one option names ("Black / of African or
  // African-American descent"): the multi-select's pick, where US_OPT_ANALYST's
  // "Asian" (three options narrow it) is left blank.
  live({ id: "r8e-gh-gusto2", ats: "greenhouse", profile: BOOTCAMP_CAREER_GAP, url: gh("gusto", "8075901") }),
  // Samsara's job page carries no form until Apply opens Greenhouse's embed:
  // the embed itself. COMPLETE_CANADIAN heard through LinkedIn, which the
  // "Select all that apply" list names (NEW_GRAD_DECLINER's "Job board" left
  // it blank by design).
  live({ id: "r8e-gh-samsara", ats: "greenhouse", profile: COMPLETE_CANADIAN, url: "https://job-boards.greenhouse.io/embed/job_app?for=samsara&token=8094371" }),

  // -------------------------------------------------- batch F: the smaller ATSs
  // Prod's active postings (2026-10-21), fewest live cases first: Gusto,
  // Eightfold and Comeet had none, Dayforce, Pinpoint and ADP one each,
  // Recruitee two. Oracle Cloud's form sits behind an emailed PIN (round 3's
  // case stops there): skipped. Eightfold's apply page loads its questions by
  // POST (/api/application/v2/questions, the answers so far in its body), which
  // the live rule blocks, so its form never renders: Qualcomm's and Boston
  // Scientific's cases were dropped after the blind run. So was ADP's Community
  // Care Plan posting: its Apply opened nothing live (NOTES, round 8).
  // Gusto's posting page only links to its form ("Apply for …", no sign-in):
  // the form itself.
  live({ id: "r8f-gusto-cortrucent", ats: "gusto", profile: BOOTCAMP_CAREER_GAP, url: "https://jobs.gusto.com/postings/cortrucent-security-ai-generalist-entry-level-48971303-29ee-4940-a7c3-b99ef5a0ffc4/applicants/new" }),
  live({ id: "r8f-gusto-vecna", ats: "gusto", profile: US_F1_STUDENT, url: "https://jobs.gusto.com/postings/vecna-ai-applied-scientist-intern-98494fff-2e4f-4ba2-be09-eb1e8e2303c3/applicants/new" }),
  live({ id: "r8f-comeet-vast", ats: "comeet", profile: INDIA_NEW_GRAD, url: "https://www.comeet.com/jobs/vastdata/43.001/top-graduate-software-engineer----v-force---rtp/F1.261", beforeFill: userOpensComeetForm }),
  live({ id: "r8f-comeet-pinnacle", ats: "comeet", profile: US_VETERAN, url: "https://www.comeet.com/jobs/pinnaclehomecare/1B.00E/st_-cloud-physical-therapist-home-health-ft/41.47D", beforeFill: userOpensComeetForm }),
  live({ id: "r8f-dayforce-york", ats: "dayforce", profile: US_H1B_SENIOR, url: "https://jobs.dayforcehcm.com/en-US/yss/candidateportal/jobs/4521" }),
  live({ id: "r8f-dayforce-woodmen", ats: "dayforce", profile: COMPLETE_CANADIAN, url: "https://jobs.dayforcehcm.com/en-US/woodmenlife/candidateportal/jobs/2855" }),
  live({ id: "r8f-pinpoint-impulse", ats: "pinpoint", profile: MONTREAL_CHANGER, url: "https://impulsespace.pinpointhq.com/en/postings/66981a77-8e79-4050-9818-4ed4209c5579" }),
  live({ id: "r8f-pinpoint-whiteswan", ats: "pinpoint", profile: UK_SENIOR, url: "https://whiteswandata.pinpointhq.com/en/postings/2d85b27e-e368-4b75-91d0-302e2904d31c" }),
  live({ id: "r8f-adp-dialight", ats: "adp", profile: NEW_GRAD_DECLINER, url: "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=162dfc0c-0a36-42b3-b562-4181df89cca8&jobId=942126" }),
  live({ id: "r8f-recruitee-huawei", ats: "recruitee", profile: BERLIN_STAFF, url: "https://huaweicanada.recruitee.com/o/intern-researcher-agentic-ai-1/c/new" }),
];
