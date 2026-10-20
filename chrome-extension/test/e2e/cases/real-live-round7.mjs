/**
 * Round 7 (2026-10-20): more Greenhouse boards, SmartRecruiters and Lever.
 * Same posture as every live case: GET and GraphQL queries only, never a
 * submit, no account.
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
const lever = (board, id) => `https://jobs.lever.co/${board}/${id}/apply`;
const sr = (company, uuid) => `https://jobs.smartrecruiters.com/oneclick-ui/company/${company}/publication/${uuid}?dcr_ci=${company}`;

export default [
  // ------------------------- batch A: Greenhouse boards in no bank and no case
  // From prod's active postings (2026-10-20): 29 of 163 live Greenhouse boards
  // were in no question bank and no case (Amperesand's posting has closed);
  // 13 of them, the ones asking the most, blind first.
  live({ id: "r7a-gh-gallup", ats: "greenhouse", profile: US_OPT_ANALYST, url: "https://job-boards.greenhouse.io/gallup/jobs/4449991009" }),
  live({ id: "r7a-gh-duolingo-uni", ats: "greenhouse", profile: INDIA_NEW_GRAD, url: "https://job-boards.greenhouse.io/duolingounirecruitment/jobs/8888785002" }),
  live({ id: "r7a-gh-nationallife", ats: "greenhouse", profile: BOOTCAMP_CAREER_GAP, url: "https://job-boards.greenhouse.io/nationallifeinsurancecompany/jobs/4449670009" }),
  live({ id: "r7a-gh-axon", ats: "greenhouse", profile: US_VETERAN, url: "https://job-boards.greenhouse.io/axontalentcommunity/jobs/8010994003" }),
  live({ id: "r7a-gh-gcm", ats: "greenhouse", profile: COMPLETE_CANADIAN, url: "https://job-boards.greenhouse.io/gcmgrosvenor/jobs/8015792003" }),
  live({ id: "r7a-gh-artefact", ats: "greenhouse", profile: US_GREENCARD_STUDENT, url: "https://job-boards.greenhouse.io/artefactlinkedin/jobs/8893475002" }),
  live({ id: "r7a-gh-inversion", ats: "greenhouse", profile: US_F1_STUDENT, url: "https://job-boards.greenhouse.io/inversionspace/jobs/4744391005" }),
  live({ id: "r7a-gh-arcesium", ats: "greenhouse", profile: US_H1B_SENIOR, url: "https://job-boards.greenhouse.io/arcesiumllc/jobs/5257176007" }),
  live({ id: "r7a-gh-perpay", ats: "greenhouse", profile: NEW_GRAD_DECLINER, url: "https://job-boards.greenhouse.io/perpay/jobs/5252881007" }),
  live({ id: "r7a-gh-alten", ats: "greenhouse", profile: MONTREAL_CHANGER, url: "https://job-boards.greenhouse.io/altentechnologyusa/jobs/5262456007" }),
  live({ id: "r7a-gh-amca", ats: "greenhouse", profile: BERLIN_STAFF, url: "https://job-boards.greenhouse.io/amca/jobs/4425120009" }),
  live({ id: "r7a-gh-vestmark", ats: "greenhouse", profile: UK_SENIOR, url: "https://job-boards.greenhouse.io/vestmark/jobs/8204509" }),
  live({ id: "r7a-gh-66degrees", ats: "greenhouse", profile: SPARSE_CANADIAN, url: "https://job-boards.greenhouse.io/66degrees/jobs/6220259004" }),

  // ------------------------------------------- batch B: SmartRecruiters, live
  // 175 active SmartRecruiters postings in prod (26 companies). Their
  // screening questions are in the GET-only bank (tools/qbank-
  // smartrecruiters.mjs); these are 13 of the bank's postings on the real
  // one-click page. SmartRecruiters' bot check lets one page through per
  // fresh browser on this machine, so each runs alone (one run per case).
  // nextPages: 1 tries the step to the screening questions; its save POSTs
  // are blocked, so a page that needs one stays where it is.
  live({ id: "r7b-sr-wellmark", ats: "smartrecruiters", profile: US_F1_STUDENT, url: sr("WellmarkInc", "32efa7a4-99ab-40e8-990d-efee4e35a12c"), nextPages: 1 }),
  live({ id: "r7b-sr-servicenow-ml", ats: "smartrecruiters", profile: US_H1B_SENIOR, url: sr("ServiceNow", "81f3aa2a-2096-47f9-b3ce-48c318a066ce"), nextPages: 1 }),
  live({ id: "r7b-sr-wd", ats: "smartrecruiters", profile: US_GREENCARD_STUDENT, url: sr("WesternDigital", "ed8ce28c-9574-492d-819d-caa4e0dbb275"), nextPages: 1 }),
  live({ id: "r7b-sr-rodeofx", ats: "smartrecruiters", profile: MONTREAL_CHANGER, url: sr("RodeoFX", "75ef52ee-d897-48e7-a6b2-6963b62e5f90"), nextPages: 1 }),
  live({ id: "r7b-sr-gdms", ats: "smartrecruiters", profile: COMPLETE_CANADIAN, url: sr("GDMSI", "5d248e97-f9c7-46da-b20f-1dd75d0cd15f"), nextPages: 1 }),
  live({ id: "r7b-sr-ccsf", ats: "smartrecruiters", profile: US_VETERAN, url: sr("CityAndCountyOfSanFrancisco1", "6466a5f2-b3c3-48b2-9030-d5056c3a1e11"), nextPages: 1 }),
  live({ id: "r7b-sr-crb", ats: "smartrecruiters", profile: BOOTCAMP_CAREER_GAP, url: sr("CRB", "cce1901a-e42c-4ad0-8da4-c424c30fca76"), nextPages: 1 }),
  live({ id: "r7b-sr-m3", ats: "smartrecruiters", profile: US_OPT_ANALYST, url: sr("M3USA", "687a7aea-bd8a-4c5c-9228-616f9c68672a"), nextPages: 1 }),
  live({ id: "r7b-sr-intuitive", ats: "smartrecruiters", profile: INDIA_NEW_GRAD, url: sr("Intuitive", "1883307b-2b51-4609-8b00-4e16db272efa"), nextPages: 1 }),
  live({ id: "r7b-sr-cricut", ats: "smartrecruiters", profile: UK_SENIOR, url: sr("Cricut", "05e9a3e6-53a6-47ac-872b-da07e41e3996"), nextPages: 1 }),
  live({ id: "r7b-sr-llnl", ats: "smartrecruiters", profile: SPARSE_CANADIAN, url: sr("LLNL", "24a9b26d-fc20-4ff6-b93f-0d4d1e99ab0a"), nextPages: 1 }),
  live({ id: "r7b-sr-bosch", ats: "smartrecruiters", profile: BERLIN_STAFF, url: sr("BoschGroup", "d3a1a061-46a5-438f-91ba-271017239373"), nextPages: 1 }),
  live({ id: "r7b-sr-veolia", ats: "smartrecruiters", profile: NEW_GRAD_DECLINER, url: sr("VeoliaEnvironnementSA", "65019996-949d-4bd7-85d7-7bfdac4f8f2d"), nextPages: 1 }),

  // P1c: SmartRecruiters' label fallback (1161e4b) reads a web component's
  // own label attribute when the scan comes before its shadow root draws
  // the <label>. On this machine the labels are always drawn first ("City*",
  // read the ordinary way), so the page runs on a CPU slowed sixfold, with
  // Autofill pressed the moment it is enabled. A label read from the
  // attribute shows without the asterisk ("City").
  live({
    id: "r7p-sr-servicenow-slow",
    ats: "smartrecruiters",
    url: "https://jobs.smartrecruiters.com/oneclick-ui/company/ServiceNow/publication/c6a55189-e797-4e24-93c8-7bd5cef0e2ef?dcr_ci=ServiceNow",
    cpuThrottle: 6,
    trigger: { fillTimeoutMs: 300000, clickImmediately: true },
  }),

  // ----------------------------------- batch C: Lever boards in no bank and no case
  // From prod's active postings (2026-10-20): 15 of 37 live Lever boards were
  // in no question bank and no case; 13 of them, blind first.
  live({ id: "r7c-lever-artera", ats: "lever", profile: US_H1B_SENIOR, url: lever("artera", "e69fa8b3-ecad-4f6a-b8b6-77d6542dfbe1") }),
  live({ id: "r7c-lever-azul", ats: "lever", profile: US_F1_STUDENT, url: lever("azul", "8f41ceaf-8835-4fc7-ba57-9384487d59db") }),
  live({ id: "r7c-lever-bhvr", ats: "lever", profile: MONTREAL_CHANGER, url: lever("bhvr", "540b00d3-8693-4c2b-9e98-10e33cb0ce62") }),
  live({ id: "r7c-lever-brillio", ats: "lever", profile: INDIA_NEW_GRAD, url: lever("brillio-2", "3de75f8b-9ddc-4908-b67d-d6e91bafd9e2") }),
  live({ id: "r7c-lever-cesiumastro", ats: "lever", profile: US_VETERAN, url: lever("CesiumAstro", "d56f6207-fc5b-421d-904d-4d2e9ff621bf") }),
  live({ id: "r7c-lever-cfsenergy", ats: "lever", profile: COMPLETE_CANADIAN, url: lever("cfsenergy", "fa87d365-ac8a-44c3-8fda-c9dec3ea8878") }),
  live({ id: "r7c-lever-fieldai", ats: "lever", profile: US_OPT_ANALYST, url: lever("field-ai", "b37569c3-d07f-4968-82dc-44020f34e54e") }),
  live({ id: "r7c-lever-galatea", ats: "lever", profile: BOOTCAMP_CAREER_GAP, url: lever("galatea-associates", "d5bceaf1-0137-4cc3-9353-72cc7a4f937b") }),
  live({ id: "r7c-lever-genbio", ats: "lever", profile: BERLIN_STAFF, url: lever("genbio", "7d0b5764-48ce-4d06-93d2-431742d35604") }),
  live({ id: "r7c-lever-rhombus", ats: "lever", profile: US_GREENCARD_STUDENT, url: lever("rhombus-systems", "59e7a5f3-b457-491d-af08-b629ad35e380") }),
  live({ id: "r7c-lever-solopulse", ats: "lever", profile: NEW_GRAD_DECLINER, url: lever("solopulseco", "1b36afea-e8eb-4cea-a6e9-0afc384d06ef") }),
  live({ id: "r7c-lever-viget", ats: "lever", profile: UK_SENIOR, url: lever("viget", "b18cc87d-fca2-485a-a0f2-ad6197db63f2") }),
  live({ id: "r7c-lever-waabi", ats: "lever", profile: SPARSE_CANADIAN, url: lever("waabi", "480d9d41-33d0-429a-8883-7f9ec305a30f") }),
];
