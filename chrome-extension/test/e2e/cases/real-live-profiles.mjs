/**
 * The same LIVE real pages, answered for different applicants, to exercise the
 * inference paths the sparse Canadian profile cannot:
 *
 *  - US_F1_STUDENT: lives in Cambridge, MA (structured address), F-1 visa,
 *    STATES "Yes" to sponsorship, has a date of birth, a finished MS.
 *    US authorization stays BLANK (an F-1's authorization is restricted, not
 *    a yes/no the profile settles); sponsorship is Yes; "live in the US" Yes.
 *  - ADDRESS_ONLY: everything in one street line, "1055 W Georgia St,
 *    Vancouver, BC V6E 3P3, Canada"; split into Address / City / Postal Code.
 *    Permanent resident of Canada → entitled to work in Canada.
 */
import { ADDRESS_ONLY, US_F1_STUDENT } from "../profiles.mjs";

const live = (c) => ({ mode: "live", trigger: { fillTimeoutMs: 180000 }, ...c });

export default [
  live({
    id: "live-workable-mindex-us-f1",
    ats: "workable",
    url: "https://apply.workable.com/mindex/j/84B10DB922/apply/",
    profile: US_F1_STUDENT,
    expect: {
      "#firstname": "Arjun",
      "#lastname": "Mehta",
      "#email": US_F1_STUDENT.email,
      "#headline": null,
      "#input_phone": { re: "617\\D*555\\D*0199" },
      // Workable guesses Address from the visitor's IP; the guess is replaced
      // with the profile's address (round 5).
      "#address": "Cambridge, MA, United States",
      "#cover_letter": null,
      "label:Salary Range": null,
      // Round 3: a co-op takes enrolled students, and her master's finished in
      // 2026 (a year-only graduation is done after June).
      "radio=QA_12563770": "NO",
      "ariaradio=Are you available to participate in a full double-block co-o": "NO",
      "radio=QA_12563778": "YES",
      "radio=QA_12457572": null,
      "aria-radiogroup~authorized to work in the United States": null,
      "radio=QA_12457573": "YES",
      "aria-radiogroup~live in the United States": "YES",
      "radio=QA_12457574": "YES",
      "aria-radiogroup~require sponsorship": "YES",
      "label:referred to this job": "No",
      "label:Where did you see this job": "Online job board",
      // Rows the fill adds itself ("+ Add" Education / Experience), when Workable renders them.
      "#school": { ifPresent: "Massachusetts Institute of Technology" },
      "#field_of_study": { ifPresent: "Computer Science" },
      "#degree": { ifPresent: "Master of Science in Computer Science" },
      "#title": { ifPresent: { re: "^(Machine Learning Intern)?$" } },
      "#company": { ifPresent: { re: "^(Acme Robotics)?$" } },
      "ariaradio=Are you able to work onsite at Mindex’s Rochester office at ": "YES",
    },
  }),
  live({
    id: "live-bamboo-armstrong-address-only",
    ats: "bamboohr",
    url: "https://armstrongfluidtechnology.bamboohr.com/careers/1001",
    profile: ADDRESS_ONLY,
    expect: {
      "#nickname_hpcsaf": null,
      "#firstName": "Sam",
      "#lastName": "Okafor",
      "#email": ADDRESS_ONLY.email,
      "#phone": { re: "604\\D*555\\D*0177" },
      "label:Address": "1055 W Georgia St",
      "label:City": "Vancouver",
      "label:Postal Code": "V6E 3P3",
      "label:Date Available": null,
      "#desiredPay": null,
      "#websiteUrl": null,
      "#linkedinUrl": ADDRESS_ONLY.linkedin,
      "radio~legally entitled to work in Canada": "Yes",
      "label:background check": null,
      "label:Link to This Job": { re: "bamboohr\\.com|^$" },
      "radio=customQuestionAnswers.yes_no_2378": "Yes",
    },
  }),
  live({
    id: "live-jobvite-actionet-us-f1",
    ats: "jobvite",
    url: "https://jobs.jobvite.com/actionet/job/ofYQzfwm/apply",
    profile: US_F1_STUDENT,
    expect: {
      "label:First Name": "Arjun",
      "label:Last Name": "Mehta",
      // The unit is the address's second line (decision 59, round 6).
      "label:Address*": "77 Massachusetts Ave",
      "label:Address2": "Apt 4B",
      "label:City": "Cambridge",
      "label:State": "Massachusetts",
      "label:Zip/Postal Code": "02139",
      "label:Email": US_F1_STUDENT.email,
      "label:Phone": { re: "617\\D*555\\D*0199" },
      "label:Desired Salary": null,
      "label:Gender": "Decline To Self Identify",
      "label:Race/Ethnicity": "Decline to Self Identify",
      "label:Disability Status": "I do not wish to answer",
      "label:Veteran Status": "Decline To Self Identify",
      "label:age 18 or older": "Yes",
      "label:Active Clearance": null,
      "label:Clearance Type": null,
      "label:highest education": "Masters",
      "label:certifications": null,
      "label:total number of years of relevant experience": "0",
      "label:current or past employee of ActioNet": "Neither",
      "label:family member": "No",
      "label:How did you hear": "ActioNet Career Page",
      "label:If referred by": null,
      "label:Job Fair": null,
      "label:conflicts of interest": "No",
      "label:non-competes": "No",
      "label:current or former government employee?": "No",
      // "If you are a current or former government employee, …?": the condition
      // is false for this applicant, and the form says so in an option.
      "label:are you currently": "I am not a current or former government employee",
      "label:recused": "I am not a current or former government employee",
      "label:procurement official": "No",
    },
  }),
];
