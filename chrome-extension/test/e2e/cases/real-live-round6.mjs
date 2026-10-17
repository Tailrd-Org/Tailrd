/**
 * Round 6 (2026-10-17): iCIMS behind its frame, Workday up to the review
 * page, Jobvite and more Greenhouse. Same posture as every live case: GET
 * and GraphQL queries only, never a submit, no account.
 */
import {
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

export default [
  // ------------------------------------------------ batch A: iCIMS, open postings
  // With round 5's eight r5c-icims-* cases (re-pointed at open postings), the
  // iCIMS batch of round 6. Each posting shows the job in a same-origin frame
  // whose Apply opens iCIMS's candidate login in that frame: an email, a
  // privacy notice, Next. Next POSTs behind hCaptcha, which live runs block,
  // so no case goes past the login and no account is ever created.
  live({
    id: "r6a-icims-libertymutual",
    ats: "icims",
    profile: US_OPT_ANALYST,
    url: "https://campus-libertymutual.icims.com/jobs/95486/2027-data-science-summer-internship-program---current-master%27s/job", nextPages: 2,
    expect: {
      "label:Email": "jiwoo.park@example.com",
      "label:I acknowledge I have read the terms of the Privacy Notice an": "checked",
    },
  }),
  live({
    id: "r6a-icims-sas",
    ats: "icims",
    profile: INDIA_NEW_GRAD,
    url: "https://careers-sas.icims.com/jobs/42964/summer-2027---software-development-and-testing-intern/job", nextPages: 2,
    expect: {
      "label:Email": "ananya.raghavan@example.in",
    },
  }),
  live({
    id: "r6a-icims-secure",
    ats: "icims",
    profile: COMPLETE_CANADIAN,
    url: "https://careers-canada-secure.icims.com/jobs/2018/junior-software-developer/job", nextPages: 2,
    expect: {
      "label:Email": "maya.tremblay@example.com",
      "label:I accept": "checked",
    },
  }),
  live({
    id: "r6a-icims-vch",
    ats: "icims",
    profile: COMPLETE_CANADIAN,
    url: "https://careers-vch.icims.com/jobs/170905/junior-business-advisor%2c-data-%26-analytics/job", nextPages: 2,
    expect: {
      "#email": "maya.tremblay@example.com",
    },
  }),
  live({
    id: "r6a-icims-lutron",
    ats: "icims",
    profile: BOOTCAMP_CAREER_GAP,
    url: "https://studentopportunities-lutron.icims.com/jobs/5616/spring-2027---software-engineering-co-op---philadelphia%2c-pa/job", nextPages: 2,
    expect: {
      "label:Email": "dandre.jb@example.com",
      "label:I acknowledge and agree to Lutron’s Recruiting Privacy Notic": "checked",
    },
  }),

  // --------------------------------------- batch B: Workday, to the account wall
  // Workday's questions load only inside a signed-in apply flow (its one
  // pre-sign-in GET, applyflowpages, lists the pages, not their questions),
  // so there is no GET-only question bank. Live, each posting goes as far as a
  // run may: the posting, Apply, the chooser, the create-account wall. Its
  // Create Account POSTs, which live runs block: no account is created.
  live({ id: "r6b-wd-analogdevices", ats: "workday", profile: COMPLETE_CANADIAN, url: "https://analogdevices.wd1.myworkdayjobs.com/External/job/Canada-Toronto/Embedded-Software-Engineer_R266615", nextPages: 2 }),
  live({ id: "r6b-wd-aep", ats: "workday", profile: US_OPT_ANALYST, url: "https://aep.wd1.myworkdayjobs.com/en-US/aepcareersite/job/Columbus-OH/Data-Scientist-Intern---Columbus--OH_R19884", nextPages: 2 }),
  live({ id: "r6b-wd-bdo", ats: "workday", profile: MONTREAL_CHANGER, url: "https://bdo.wd3.myworkdayjobs.com/Bdo/job/Saskatoon---2nd-Avenue-South/Co-op-or-Intern--Assurance---Saskatoon--Summer-2027-_JR7038", nextPages: 2 }),
  live({ id: "r6b-wd-abb", ats: "workday", profile: INDIA_NEW_GRAD, url: "https://abb.wd3.myworkdayjobs.com/en-US/external_career_page/job/USA-NC-Cary/AI-Engineering--Business-Systems-Intern--Summer-2027_JR00048714", nextPages: 2 }),
  live({ id: "r6b-wd-homedepot", ats: "workday", profile: SPARSE_CANADIAN, url: "https://homedepot.wd5.myworkdayjobs.com/en-US/careerdepotcanada/job/CANADA-STORE-SUPPORT-CENTER---7000/Intern--Full-Stack-Software-Developer_Req195012", nextPages: 2 }),
  live({ id: "r6b-wd-adobe", ats: "workday", profile: US_F1_STUDENT, url: "https://adobe.wd5.myworkdayjobs.com/external_experienced/job/New-York/XMLNAME-2027-Intern---Enterprise-Architecture-Analyst_R171856", nextPages: 2 }),
  live({ id: "r6b-wd-bose", ats: "workday", profile: US_GREENCARD_STUDENT, url: "https://boseallaboutme.wd503.myworkdayjobs.com/en-US/bose_careers/job/US-MA---Framingham/Embedded-Software-Engineer-Co-op----Audio-Tech_R29278", nextPages: 2 }),
  live({ id: "r6b-wd-revvity", ats: "workday", profile: BOOTCAMP_CAREER_GAP, url: "https://revvity.wd103.myworkdayjobs.com/en-US/external/job/Akron/Software-Integration-Co-Op--Spring-2027-_JR-045616", nextPages: 2 }),
  live({ id: "r6b-wd-copart", ats: "workday", profile: US_H1B_SENIOR, url: "https://copart.wd12.myworkdayjobs.com/en-US/copart/job/Dallas-TX---Headquarters/Data---AI-Intern_JR111596", nextPages: 2 }),
  live({ id: "r6b-wd-centric", ats: "workday", profile: COMPLETE_CANADIAN, url: "https://centricsoftware.wd501.myworkdayjobs.com/en-US/centric/job/Toronto-ON/Azure-Cloud-Engineer_JR397", nextPages: 2 }),
  live({ id: "r6b-wd-expedia", ats: "workday", profile: US_VETERAN, url: "https://expedia.wd108.myworkdayjobs.com/en-US/private/job/Washington---Seattle-Campus/Software-Development-Engineering-Intern---2027---Austin--San-Jose--Seattle_R-110311", nextPages: 2 }),
  live({ id: "r6b-wd-cmu", ats: "workday", profile: NEW_GRAD_DECLINER, url: "https://cmu.wd115.myworkdayjobs.com/en-US/sei/job/Pittsburgh-PA/AI-Security-Software-Engineer-Intern---Summer-2027_2025200-1", nextPages: 2 }),
  live({ id: "r6b-wd-symbotic", ats: "workday", profile: UK_SENIOR, url: "https://symbotic.wd504.myworkdayjobs.com/en-US/symbotic/job/USA-Wilmington--MA---HQ/Intern--Machine-Learning-Ops_R8202", nextPages: 2 }),

  // ------------------------- batch C: Greenhouse boards in no bank and no case
  // From prod's active postings (2026-10-17): 37 of 117 live Greenhouse boards
  // were in no question bank and no case; 13 of them, blind first.
  live({ id: "r6c-gh-astera", ats: "greenhouse", profile: COMPLETE_CANADIAN, url: "https://job-boards.greenhouse.io/asteraearlycareer2027/jobs/4733856005" }),
  live({ id: "r6c-gh-kensington", ats: "greenhouse", profile: MONTREAL_CHANGER, url: "https://job-boards.greenhouse.io/kensingtontours/jobs/5262680007" }),
  live({ id: "r6c-gh-tenstorrent", ats: "greenhouse", profile: SPARSE_CANADIAN, url: "https://job-boards.greenhouse.io/tenstorrentuniversity/jobs/5256568007" }),
  live({ id: "r6c-gh-figureai", ats: "greenhouse", profile: US_H1B_SENIOR, url: "https://job-boards.greenhouse.io/figureai/jobs/4722086006" }),
  live({ id: "r6c-gh-heygen", ats: "greenhouse", profile: US_OPT_ANALYST, url: "https://job-boards.greenhouse.io/heygen/jobs/5265289007" }),
  live({ id: "r6c-gh-point72", ats: "greenhouse", profile: US_F1_STUDENT, url: "https://boards.greenhouse.io/point72/jobs/8885299002?gh_jid=8885299002" }),
  live({ id: "r6c-gh-purestorage", ats: "greenhouse", profile: US_VETERAN, url: "https://job-boards.greenhouse.io/purestorage/jobs/8249749" }),
  live({ id: "r6c-gh-sigma", ats: "greenhouse", profile: BOOTCAMP_CAREER_GAP, url: "https://job-boards.greenhouse.io/sigmacomputing/jobs/8015269003" }),
  live({ id: "r6c-gh-justworks", ats: "greenhouse", profile: US_GREENCARD_STUDENT, url: "https://boards.greenhouse.io/justworks/jobs/8236815?gh_jid=8236815" }),
  live({ id: "r6c-gh-hasbro", ats: "greenhouse", profile: NEW_GRAD_DECLINER, url: "https://job-boards.greenhouse.io/hasbro/jobs/4439483009" }),
  live({ id: "r6c-gh-clear", ats: "greenhouse", profile: INDIA_NEW_GRAD, url: "https://job-boards.greenhouse.io/clear/jobs/8241829" }),
  live({ id: "r6c-gh-arc", ats: "greenhouse", profile: UK_SENIOR, url: "https://job-boards.greenhouse.io/arcinstitute/jobs/6222737004" }),
  live({ id: "r6c-gh-pdt", ats: "greenhouse", profile: US_OPT_ANALYST, url: "https://job-boards.greenhouse.io/pdtpartners/jobs/8263031" }),
];
