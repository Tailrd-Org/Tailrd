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

export default [
  // ------------------------- batch A: SmartRecruiters, to the screening step
  // What held round 7's postings on page 1 is the posting's own required
  // résumé ("Please attach your resume to complete this application"): the
  // one-click context says so (`features.resumeRequired`), and a run uploads
  // nothing. These 13 prod postings (2026-10-21) take the résumé as optional,
  // so Next opens their screening step with no upload (EVERSANA's and
  // LinkedIn's require experience instead). Each runs in a browser of its own.
  live({ id: "r8a-sr-servicenow-assoc", ats: "smartrecruiters", profile: US_F1_STUDENT, url: sr("ServiceNow", "749de8bb-34ce-4a49-b2d8-48c535bc9956"), nextPages: 1 }),
  live({ id: "r8a-sr-servicenow-swe", ats: "smartrecruiters", profile: US_GREENCARD_STUDENT, url: sr("ServiceNow", "611bfd47-bd06-4eb3-99e3-e307dff74ce5"), nextPages: 1 }),
  live({ id: "r8a-sr-servicenow-fdrp", ats: "smartrecruiters", profile: UK_SENIOR, url: sr("ServiceNow", "6efc8740-a1e7-4f73-859b-4736906924d9"), nextPages: 1 }),
  live({ id: "r8a-sr-servicenow-finance", ats: "smartrecruiters", profile: NEW_GRAD_DECLINER, url: sr("ServiceNow", "b3ba4b29-9112-4183-b619-aa51ac29f67e"), nextPages: 1 }),
  live({ id: "r8a-sr-intuitive-fs1", ats: "smartrecruiters", profile: US_H1B_SENIOR, url: sr("Intuitive", "07f9a52b-ba30-4629-abc9-a64c18f4ae43"), nextPages: 1 }),
  live({ id: "r8a-sr-intuitive-fs2", ats: "smartrecruiters", profile: US_VETERAN, url: sr("Intuitive", "32a72cca-295c-4266-b854-dd0240ba1146"), nextPages: 1 }),
  live({ id: "r8a-sr-bosch-facility", ats: "smartrecruiters", profile: BOOTCAMP_CAREER_GAP, url: sr("BoschGroup", "7b3825cb-cb81-4256-9b8c-581fe371c392"), nextPages: 1 }),
  live({ id: "r8a-sr-bosch-hr", ats: "smartrecruiters", profile: COMPLETE_CANADIAN, url: sr("BoschGroup", "6ba6cf3a-ecce-44d2-9681-7d21f4dac817"), nextPages: 1 }),
  live({ id: "r8a-sr-bosch-calibration", ats: "smartrecruiters", profile: INDIA_NEW_GRAD, url: sr("BoschGroup", "3913d946-2d13-4ea4-8a56-b36ac5a3c585"), nextPages: 1 }),
  live({ id: "r8a-sr-bostoneng", ats: "smartrecruiters", profile: US_OPT_ANALYST, url: sr("BostonEngineeringCorporation", "d4123676-8efd-4b15-9c06-d08e248b1e9d"), nextPages: 1 }),
  live({ id: "r8a-sr-circle", ats: "smartrecruiters", profile: MONTREAL_CHANGER, url: sr("CircleLogistics1", "d20c0deb-3cf7-44d2-b521-9aa234660fa7"), nextPages: 1 }),
  live({ id: "r8a-sr-eversana", ats: "smartrecruiters", profile: BERLIN_STAFF, url: sr("EVERSANA1", "c72e43d8-e37d-4233-bb39-f2b394d6aea4"), nextPages: 1 }),
  live({ id: "r8a-sr-linkedin", ats: "smartrecruiters", profile: SPARSE_CANADIAN, url: sr("LinkedIn3", "001359e4-c46c-45cb-a388-bc79014089ec"), nextPages: 1 }),
  // The context's `features` are the company's defaults, not the posting's:
  // its form's own configuration (the page's GET of `.../config`) required a
  // résumé on Bosch's three and Circle's, and experience on EVERSANA's and
  // LinkedIn's. Of prod's 26 SmartRecruiters companies only ServiceNow,
  // Intuitive and Western Digital take a posting with nothing to upload; four
  // more of ServiceNow's, with round 7's three, make thirteen screening steps.
  live({ id: "r8a-sr-servicenow-finance2", ats: "smartrecruiters", profile: US_OPT_ANALYST, url: sr("ServiceNow", "99e904e8-1e5a-4096-beb0-5996c4cf5427"), nextPages: 1 }),
  live({ id: "r8a-sr-servicenow-fdrp2", ats: "smartrecruiters", profile: INDIA_NEW_GRAD, url: sr("ServiceNow", "60e192cf-cbad-42ef-a2d1-b2fd6a780a3c"), nextPages: 1 }),
  live({ id: "r8a-sr-servicenow-moveworks", ats: "smartrecruiters", profile: COMPLETE_CANADIAN, url: sr("ServiceNow", "e48bcb74-d711-4d5e-b486-a19a247237dd"), nextPages: 1 }),
  live({ id: "r8a-sr-servicenow-staff", ats: "smartrecruiters", profile: BERLIN_STAFF, url: sr("ServiceNow", "64dd40e1-94d0-4d8f-b6b5-acb2f9c85416"), nextPages: 1 }),
];
