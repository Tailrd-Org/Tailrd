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
