/**
 * HELD-OUT live pages (round 1 below; round 2 at the end). Round 1: twelve
 * postings drawn at random (md5 order) from prod
 * `scraped_jobs` on 2026-10-03, AFTER the night's fixes, excluding every
 * company already in the corpus. The first run was blind (no expectations,
 * every write reviewed by hand); it found five wrong writes, now fixed and
 * pinned here:
 *
 *  - Ramp (Ashby): graduation year typed into a date picker → "12/31/2026"
 *  - Superhuman (Ashby), ZipRecruiter (Greenhouse): a bare year "2027" picked
 *    one of two options it fits
 *  - Twitch, Astranis (Greenhouse): the education row's start/end dates got
 *    the first JOB's dates
 *  - Astranis (Greenhouse): "Mechanical Engineering" for a Mechatronics student
 *  - Palantir (Lever): "High School Name" / its year got the university's
 *
 * `{ oneOf: [answer, ""] }` marks a field where blank is a miss, not a wrong
 * write (an answer the profile settles but the fill may not reach), so the
 * case pins "never a different value" without being flaky.
 */
import { SPARSE_CANADIAN } from "../profiles.mjs";

const P = SPARSE_CANADIAN;
const NAME = "Maya Tremblay";
const PHONE = { re: "416\\D*555\\D*0142" };
const TORONTO = { re: "toronto" };
const LINKEDIN = P.linkedin;
const DIAL_CODE_CANADA = { re: String.raw`canada|^\+\s*1$` };
const orBlank = (v) => ({ oneOf: [v, ""] });

const live = (c) => ({ mode: "live", profile: P, trigger: { fillTimeoutMs: 180000 }, ...c });

export default [
  // ---------------------------------------------------------------- Ashby
  // live-heldout-ashby-superhuman retired 2026-10-17: the posting closed (Ashby's job-board API no longer lists it).
  live({
    id: "live-heldout-ashby-beacon",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/beaconsoftware/be0e101c-faba-41c2-996e-d70e98b9d4ee/application",
    expect: {
      // Ashby's Yes/No questions, answered since round 5 (read 2026-10-08). The
      // office is in Toronto, where the applicant lives.
      "yesno~Are you comfortable working onsite four days a week at one o": "Yes",
      "#_systemfield_name": NAME,
      "label:Email": P.email,
      "label:Phone Number": PHONE,
      "label:LinkedIn Profile": LINKEDIN,
      "label:Start typing": TORONTO,
      "label:How did you hear about Beacon?": "Online job board",
      "label:What is your current age?": null,
      "label:What is your gender identity?": "I prefer not to answer",
      "radio~Do you identify as transgender?": "I prefer not to answer",
      "all:checkbox~I prefer not to answer": "checked",
    },
  }),
  live({
    id: "live-heldout-ashby-gecko",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/gecko-robotics/c097505b-0a28-4a33-a917-268f463641e8/application",
    expect: {
      // "How did you hear about Gecko?" with no source stated: the first of job
      // board, company site, search engine the list offers (a Tailrd user found it
      // through an aggregator). Answered since round 5's typeahead reading.
      "label=Start typing...": "Search Engine",
      "label:Full Legal Name": NAME,
      "label:Preferred Name": "Maya",
      "label:Email": P.email,
      "label:I acknowledge that Gecko has an in-office culture": "Yes, I am able and willing to work in the office location listed in the job description.",
      "label:Input gender": "Decline to self-identify",
      "label:Veteran Status": "I decline to self-identify for protected veteran status",
      "radio~Race": "Decline to self-identify",
    },
  }),
  live({
    id: "live-heldout-ashby-ramp",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/ramp/b39ceb08-a0a7-4f8b-a760-2fb88e209956/application",
    expect: {
      // Ashby's Yes/No questions, answered since round 5 (read 2026-10-08).
      // Relocating to the posting's city for the internship is its requirement,
      // accepted by applying (no move refused on the profile).
      "yesno~Are you currently pursuing a Bachelor’s, Master’s, or PhD in": "Yes",
      "yesno~Are you willing to relocate the greater NYC area for the dur": "Yes",
      "label:Legal Name": NAME,
      "label:Preferred First Name": "Maya",
      "label:Pronouns": null,
      "label:Email": P.email,
      "tel~Phone": PHONE,
      // The SMS-consent radio Ashby labels "Phone": never consented for the user.
      "radio~Phone": "No - I do not consent to receiving text messages",
      "label:LinkedIn Profile": LINKEDIN,
      "label:Github Profile": null,
      "label:Search schools": "University of Waterloo",
      "label:Field of Study": "Mechatronics Engineering",
      "#_systemfield_education_history-degree": "Bachelor of Applied Science in Mechatronics Engineering",
      // "What is your graduation date?", a react-datepicker: the year alone
      // became 12/31/2026.
      "label:Pick date": null,
      "label:Show us something you": null,
      "#_systemfield_education_history-isCurrent": "checked",
    },
  }),
  // ----------------------------------------------------------- Greenhouse
  live({
    id: "live-heldout-gh-spacex",
    ats: "greenhouse",
    url: "https://boards.greenhouse.io/spacex/jobs/8622574002?gh_jid=8622574002",
    expect: {
      "label:First Name": "Maya",
      "label:Last Name": "Tremblay",
      "#preferred_name": "Maya",
      "label:Email": P.email,
      "#country": DIAL_CODE_CANADA,
      "tel~Phone": PHONE,
      "label:Location (City)": TORONTO,
      "label:School": "University of Waterloo",
      "label:Degree": "Bachelor's Degree",
      "label:Discipline": "Mechatronics Engineering",
      "label:LinkedIn Profile": LINKEDIN,
      "label:How did you hear about this job?": "Careers site",
      "label:GPA (Undergraduate)": null,
      // No doctorate, so its GPA question does not apply.
      "label:GPA (Doctorate)": "Not applicable/Do not recall",
      "label:SAT Score": "Did not take/Do not recall",
      "label:Active Security Clearance": null,
      "label:Are you legally authorized to work in the United States?": null,
      "label:Citizenship Status": null,
      "label:Gender": "Decline To Self Identify",
      "label:Veteran Status": "I don't wish to answer",
      "#question_37213461002": "Yes",
      "#hispanic_ethnicity": "Decline To Self Identify",
      "#question_37213454002": "Other/Not Applicable",
      "#question_37213457002": "Did not take/Do not recall",
      "#question_37213458002": "Did not take/Do not recall",
      "#question_37213460002": "I have never worked for SpaceX, SpaceXAI, xAI, X, or Twitter",
    },
  }),
  live({
    id: "live-heldout-gh-twitch",
    ats: "greenhouse",
    url: "https://job-boards.greenhouse.io/twitch/jobs/8700578002",
    expect: {
      // Round 3: "permanent resident after your latest citizenship?" is No for a
      // citizen living in her own country.
      "#question_37744079002": "No",
      "label:First Name": "Maya",
      "label:Last Name": "Tremblay",
      "label:Email": P.email,
      "#country": DIAL_CODE_CANADA,
      "tel~Phone": PHONE,
      "label:Location (City)": TORONTO,
      "label:School": "University of Waterloo",
      "label:Degree": "Bachelor's Degree",
      // No Mechatronics option: the broad parent discipline, or nothing.
      "label:Discipline": orBlank("Engineering"),
      // The EDUCATION row's dates: unknown start, graduation year as the end.
      "#start-month--0": null,
      "#start-year--0": null,
      "#end-month--0": null,
      "#end-year--0": "2027",
      "label:LinkedIn Profile": LINKEDIN,
      "label:Are you currently a Twitch employee?": orBlank("No"),
      "label:Are you a current employee with Amazon": "No",
      "label:Have you previously been employed by Amazon": "No",
      "label:Have you previously applied to Amazon": "No",
      "label:Are you open to relocation?": null,
      "label:If offered employment by Amazon, would you be legally eligible": null,
      "label:In which country/region do you have citizenship?": "Canada",
      // "For the sole purpose of determining export licensing requirements, …
      // country of citizenship": Canada.
      "#question_37744080002": "Canada",
      "label:What is your expected total base pay": null,
      "label:Gender": "Decline To Self Identify",
      "#question_37744074002": "No",
      "#question_37744082002": "Yes",
      "#hispanic_ethnicity": "Decline To Self Identify",
      "#veteran_status": "I don't wish to answer",
      "#question_37744077002": "No",
    },
  }),
  live({
    id: "live-heldout-gh-ziprecruiter",
    ats: "greenhouse",
    url: "https://job-boards.greenhouse.io/ziprecruiter/jobs/8180455",
    expect: {
      "label:First Name": "Maya",
      "label:Last Name": "Tremblay",
      "label:Email": P.email,
      "#country": DIAL_CODE_CANADA,
      "tel~Phone": PHONE,
      "label:Location (City)": TORONTO,
      // Its react-select failed to open in 1 run of 5 (a miss, never wrong).
      "#school--0": orBlank("University of Waterloo"),
      "label:Degree": "Bachelor's Degree",
      "label:What school are you currently attending": "University of Waterloo",
      // Buckets "December 2026 - November 2027" | "December 2027 - November
      // 2028": a bare 2027 fits both.
      "label:When is your anticipated graduation date?": null,
      "label:Are you legally authorized to work in the United States": null,
      "label:Will you now, or in the future, require sponsorship": null,
      "label:What archetype are you most closely aligned with?": null,
      "label:How did you hear about this opportunity?": "LinkedIn",
      "#gender": "Decline To Self Identify",
      "#hispanic_ethnicity": "Decline To Self Identify",
      "#veteran_status": "I don't wish to answer",
    },
  }),
  live({
    id: "live-heldout-gh-astranis",
    ats: "greenhouse",
    url: "https://job-boards.greenhouse.io/astranis/jobs/4704716006",
    expect: {
      "label:First Name": "Maya",
      "label:Last Name": "Tremblay",
      "label:Email": P.email,
      "#country": DIAL_CODE_CANADA,
      "tel~Phone": PHONE,
      "label:Location (City)": TORONTO,
      "label:School": "University of Waterloo",
      "label:Degree": "Bachelor's Degree",
      // Was "Mechanical Engineering": a shared stem is not the same discipline.
      "label:Discipline": "Other",
      "#start-month--0": null,
      "#start-year--0": null,
      "#end-month--0": null,
      "#end-year--0": "2027",
      "label:What is the most impressive thing you have ever accomplished?": null,
      "label:Please confirm the season you are applying for.": "Summer 2027",
      "label:When are you able to join Astranis as an intern?": null,
      "label:How did you hear about Astranis?": "Online job board",
      "label:LinkedIn Profile": LINKEDIN,
      "label:By selecting YES, I consent to receive recruiting SMS": "No",
      "#question_8623876006": "Yes",
      "#gender": "Decline To Self Identify",
      "#hispanic_ethnicity": "Decline To Self Identify",
      "#veteran_status": "I don't wish to answer",
    },
  }),
  // ---------------------------------------------------------------- Lever
  live({
    id: "live-heldout-lever-palantir",
    ats: "lever",
    url: "https://jobs.lever.co/palantir/d5486403-c050-4920-b2e0-91b69b61ebb2/apply",
    expect: {
      // No disability answer stated: the form's decline ("I do not want to answer"
      // was not recognized as one until 2026-10-04), signed like any answer.
      "#disabilitySelectElement": "I do not want to answer",
      "name=eeo[disabilitySignature]": "Maya Tremblay",
      "name=eeo[disabilitySignatureDate]": { re: String.raw`^\d{2}/\d{2}/\d{4}$` },
      "label:Full name": NAME,
      "label:Email": P.email,
      "label:Phone": PHONE,
      "label:Current location": { re: "^(Toronto, ON, CAN)?$" },
      "label:Current company": "Kinaxis",
      // Lever cards labelled only "Type your response": Preferred Name.
      "name=cards[a69a985a-eae9-4c14-90fb-b5a4b891523e][field1]": "Maya",
      "label:LinkedIn URL": LINKEDIN,
      "label:High School Name": null,
      "label:Year of High School Graduation": null,
      "label:Which university are you currently attending": "University of Waterloo",
      "label:Please include your intended graduation year": "2027",
      "label:What is your major?": "Other",
      "label:Are you legally authorized to work in the country for which you are applying": null,
      "label:Will you now or in the future require sponsorship": null,
      "label:Please tell us how you heard about this internship": "Job Board (Indeed, Monster, etc.)",
      "name=eeo[veteran]": "I decline to self-identify for protected veteran status",
    },
  }),
  live({
    id: "live-heldout-lever-shieldai",
    ats: "lever",
    url: "https://jobs.lever.co/shieldai/87d982f2-8b2b-4c73-9a19-71e461c7b724/apply",
    expect: {
      // No disability answer stated: the form's decline ("I do not want to answer"
      // was not recognized as one until 2026-10-04), signed like any answer.
      "#disabilitySelectElement": "I do not want to answer",
      "name=eeo[disabilitySignature]": "Maya Tremblay",
      "name=eeo[disabilitySignatureDate]": { re: String.raw`^\d{2}/\d{2}/\d{4}$` },
      "label:Which location are you applying for?": null,
      "label:Full name": NAME,
      "label:Email": P.email,
      "label:Phone": PHONE,
      "label:Current location": { re: "^(Toronto, ON, CAN)?$" },
      "label:Current company": "Kinaxis",
      // "Please state your full legal name:" (a card labelled "Type your response").
      "name=cards[eb3750bb-e1f0-473c-a54d-012919c5e349][field0]": NAME,
      "label:LinkedIn URL": LINKEDIN,
      "label:Are you authorized to work in the United States?": null,
      "label:Will you require sponsorship for employment now or in the future?": null,
      "label:How did you hear about us?": "Handshake",
      "label:Are you local to or willing to relocate?": "Yes",
      "name=eeo[gender]": "Decline to self-identify",
      "name=eeo[veteran]": "I decline to self-identify for protected veteran status",
    },
  }),
  live({
    id: "live-heldout-lever-hermeus",
    ats: "lever",
    url: "https://jobs.lever.co/hermeus/78008094-ca81-4a0c-9a18-93b30f932acd/apply",
    expect: {
      // No disability answer stated: the form's decline ("I do not want to answer"
      // was not recognized as one until 2026-10-04), signed like any answer.
      "#disabilitySelectElement": "I do not want to answer",
      "name=eeo[disabilitySignature]": "Maya Tremblay",
      "name=eeo[disabilitySignatureDate]": { re: String.raw`^\d{2}/\d{2}/\d{4}$` },
      // Round 3: a finished internship in the history, named.
      "name=cards[ebaebe01-2bfb-4932-9a01-0582fdb9b10e][field0]": "Yes, Software Developer Intern at Shopify (2025-01 to 2025-04)",
      "label:Full name": NAME,
      "label:Email": P.email,
      "label:Phone": PHONE,
      "label:Current location": { re: "^(Toronto, ON, CAN)?$" },
      "label:Current company": "Kinaxis",
      // Cards: Legal First Name, Legal Last Name, Preferred First Name, Nick Name.
      "name=cards[c6cebbb8-69f4-49e4-b460-65e8a2086404][field0]": "Maya",
      "name=cards[c6cebbb8-69f4-49e4-b460-65e8a2086404][field1]": "Tremblay",
      "name=cards[c6cebbb8-69f4-49e4-b460-65e8a2086404][field2]": "Maya",
      "name=cards[c6cebbb8-69f4-49e4-b460-65e8a2086404][field3]": null,
      "label:LinkedIn URL": LINKEDIN,
      "label:Are you legally authorized to work for any employer in the United States": null,
      // Fall 2026 | Spring 2027 | Summer 2027 | Fall 2027 …: a year is not a season.
      "label:When do you expect to graduate?": null,
      "label:What degree are you pursuing?": "Bachelor’s",
      "label:What is your location?": "Canada",
      "label:What is your age range?": null,
      "label:Do you require visa sponsorship for employment in the USA?": null,
      "name=eeo[gender]": "Decline to self-identify",
      "name=eeo[race]": "Decline to self-identify",
      "name=eeo[veteran]": "I decline to self-identify for protected veteran status",
      "radio=cards[73621184-c538-4979-b0ff-adda453dcd30][field0]": "Company Website",
    },
  }),
  // ------------------------------------------------------------- Workable
  // live-heldout-workable-tsa retired 2026-10-03: the posting was taken down
  // (Workable's API answers 404 "Job not found"; the page renders no form).
  // ============================================================ ROUND 2
  // Twelve more postings, drawn the same way after round 1 was fixed. The blind
  // pass found ONE wrong write (the Robinhood consent below), now fixed.
  live({
    id: "live-heldout2-ashby-npx",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/npx/048ca8da-bfb9-4454-8147-ac9497629634/application",
    expect: {
      // Ashby's Yes/No questions, answered since round 5 (read 2026-10-08).
      "yesno~Are you legally eligible to work in Canada?": "Yes",
      "yesno~Are you comfortable with the travel requirements listed in t": "Yes",
      "label:Preferred Full Name": "Maya Tremblay",
      "label:Legal Full Name": "Maya Tremblay",
      "label:Email": "maya.tremblay@example.com",
      "label:Phone number": PHONE,
      "label=Start typing...": TORONTO,
      "label:What are you looking for as an hourly rate?": null,
    },
  }),
  live({
    id: "live-heldout2-ashby-megazone",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/megazone/e2889469-cf20-4227-bf24-2a6e885f8dca/application",
    expect: {
      // Ashby's Yes/No questions, answered since round 5 (read 2026-10-08). On
      // site in Rochester, NY: the posting's requirement, accepted by applying
      // (no move refused on the profile).
      "yesno~This role requires working onsite at our facility in Rochest": "Yes",
      "label:Name": "Maya Tremblay",
      "label:Phone": PHONE,
      "label:Email": "maya.tremblay@example.com",
      "label=Start typing...": TORONTO,
      "radio~If applicable, when do you estimate you will require employe": null,
      "label:What are your hourly pay requirements": null,
      // "(Term & Year)": the profile knows only the year.
      "label:What is your expected graduation date (Term & Year)?": orBlank("2027"),
      "label:Please provide the full URL to your Linkedin profile": "https://www.linkedin.com/in/maya-tremblay",
      "radio~Input gender": "Decline to self-identify",
      "radio~Race": "Decline to self-identify",
      "radio~Veteran Status": "I decline to self-identify for protected veteran status",
    },
  }),
  live({
    id: "live-heldout2-ashby-bree",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/bree/42fe78c1-e73f-4918-bf71-776b8142112b/application",
    expect: {
      // Ashby's Yes/No questions, answered since round 5 (read 2026-10-08): a
      // Toronto applicant, a Toronto office.
      "yesno~Are you located in the Greater Toronto Area, within commutin": "Yes",
      "yesno~Are you able to come in office 3x per week (Mon, Wed, Thurs)": "Yes",
      "label:Name": "Maya Tremblay",
      "label:Email": "maya.tremblay@example.com",
      "label:What is your expectation for total annual cash compensation ": null,
    },
  }),
  live({
    id: "live-heldout2-ashby-rhoda",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/rhoda-ai/9a57c8ff-dd2b-4547-a46a-44658a699ba5/application",
    expect: {
      "label:Name": "Maya Tremblay",
      "label:Email": "maya.tremblay@example.com",
    },
  }),
  live({
    id: "live-heldout2-gh-gitai",
    ats: "greenhouse",
    url: "https://job-boards.greenhouse.io/gitai/jobs/5437128008",
    expect: {
      "label:First Name": "Maya",
      "label:Last Name": "Tremblay",
      "label:Preferred First Name": "Maya",
      "label:Email": "maya.tremblay@example.com",
      "#country": DIAL_CODE_CANADA,
      "label:Phone": PHONE,
      "label:School": "University of Waterloo",
      "label:Degree": "Bachelor's Degree",
      "label:Discipline": orBlank("Engineering"),
      "label:End date year": "2027",
      "label:Are you a U.S. citizen?": null,
      "#question_18815203008": "Yes",
    },
  }),
  live({
    id: "live-heldout2-gh-anthropic",
    ats: "greenhouse",
    url: "https://job-boards.greenhouse.io/anthropic/jobs/5319696008",
    expect: {
      "label:First Name": "Maya",
      "label:Last Name": "Tremblay",
      "label:Email": "maya.tremblay@example.com",
      "#country": DIAL_CODE_CANADA,
      "label:Phone": PHONE,
      "label:LinkedIn Profile": "https://www.linkedin.com/in/maya-tremblay",
      "label:Do you require visa sponsorship?": null,
      "label:Will you now or will you in the future require employment vi": null,
      "label:What is the address from which you plan on working? If you w": orBlank("Toronto, ON, Canada"),
      "label:Please read the arbitration agreement below": "I will read the arbitration agreement below.",
      // Agreeing to arbitrate is the applicant's alone (decision 51, round 5).
      "label:Agreement to Arbitrate": null,
      "label:Gender": "Decline To Self Identify",
      "label:Are you Hispanic/Latino?": "Decline To Self Identify",
      "label:Veteran Status": "I don't wish to answer",
      "#question_17440917008": "Yes",
      "#question_17440925008": "Yes",
      "#question_17440927008": "No",
      "#question_17440920008": "Yes",
    },
  }),
  live({
    id: "live-heldout2-gh-figma",
    ats: "greenhouse",
    url: "https://boards.greenhouse.io/figma/jobs/6143238004?gh_jid=6143238004",
    expect: {
      "label:First Name": "Maya",
      "label:Last Name": "Tremblay",
      "label:Email": "maya.tremblay@example.com",
      "#country": DIAL_CODE_CANADA,
      "label:Phone": PHONE,
      "label:Location (City)": TORONTO,
      "label:Preferred First Name": "Maya",
      "label:LinkedIn Profile": "https://www.linkedin.com/in/maya-tremblay",
      "label:How did you connect with us?": "Other",
      "label:Are you authorized to work in the country for which you appl": null,
      "label:Have you ever worked for Figma before, as an employee or a c": "No",
      "label:Gender": "Decline To Self Identify",
      "label:Are you Hispanic/Latino?": "Decline To Self Identify",
      "label:Veteran Status": "I don't wish to answer",
    },
  }),
  // live-heldout2-gh-robinhood retired 2026-10-08: the posting closed (Greenhouse's job API answers 404).
  live({
    id: "live-heldout2-lever-zoox",
    ats: "lever",
    url: "https://jobs.lever.co/zoox/5f10dfaf-5920-4506-8a09-b39b29e6f48b/apply",
    expect: {
      // "Do you plan to continue working for other employers during your contract at Zoox?": No (decision 64, round 6).
      "name=cards[18631c8a-d2a4-41d9-ba8a-8fccf4193494][field3]": "No",
      "label:Full name": "Maya Tremblay",
      "label:Email": "maya.tremblay@example.com",
      "label:Phone": PHONE,
      "label:Current location": { re: "^(Toronto, ON, CAN)?$" },
      "label:Current company": "Kinaxis",
      "label:LinkedIn URL": "https://www.linkedin.com/in/maya-tremblay",
      "radio~Do you currently receive any active funding (e.g., grants, s": null,
      "label:GenderSelect ...MaleFemaleDecline to self-identify": "Decline to self-identify",
      "label:RaceSelect ...Hispanic or LatinoWhite (Not Hispanic or Latin": "Decline to self-identify",
      "label:Veteran statusSelect ...I am a veteranI am not a veteranDecl": "Decline to self-identify",
      // Round 3: the checkbox question "currently enrolled…?" has its label now (Yes);
      // "a school schedule that allows part-time work…?" is the student's to say.
      "name=cards[18631c8a-d2a4-41d9-ba8a-8fccf4193494][field0]": "checked",
      "name=cards[18631c8a-d2a4-41d9-ba8a-8fccf4193494][field1]": null,
    },
  }),
  live({
    id: "live-heldout2-lever-pcc-canada",
    ats: "lever",
    url: "https://jobs.lever.co/pointclickcare/e56d6df3-16fb-4652-9d97-c6140700d2e0/apply",
    expect: {
      "label:Full name": "Maya Tremblay",
      "label:Email": "maya.tremblay@example.com",
      "label:Phone": PHONE,
      "label:Current location": { re: "^(Toronto, ON, CAN)?$" },
      "label:Current company": "Kinaxis",
      "label:LinkedIn URL": "https://www.linkedin.com/in/maya-tremblay",
      "radio~Gender Identity": "Prefer not to disclose",
      "radio~Do you identify as a member of the LGBTQ+ Community?": "Prefer not to disclose",
      "radio~Race/Ethnicity": "Prefer not to disclose",
      "radio~Veteran Status": "Prefer not to disclose",
      "radio~Disability": "I Don’t Wish To Answer",
    },
  }),
  live({
    id: "live-heldout2-lever-pcc-analyst",
    ats: "lever",
    url: "https://jobs.lever.co/pointclickcare/4df748a3-2864-4d8c-8393-85b185b16fa4/apply",
    expect: {
      "label:Full name": "Maya Tremblay",
      "label:Email": "maya.tremblay@example.com",
      "label:Phone": PHONE,
      "label:Current location": { re: "^(Toronto, ON, CAN)?$" },
      "label:Current company": "Kinaxis",
      "label:LinkedIn URL": "https://www.linkedin.com/in/maya-tremblay",
      "radio~Are you legally authorized to work in the US for our company": null,
      "radio~Do you now, or will you in the future, require sponsorship f": null,
      "radio~Gender Identity": "Prefer not to disclose",
      "radio~Do you identify as a member of the LGBTQ+ Community?": "Prefer not to disclose",
      "radio~Race/Ethnicity": "Prefer not to disclose",
      "radio~Veteran Status": "Prefer not to disclose",
      "radio~Disability": "I Don’t Wish To Answer",
    },
  }),
  live({
    id: "live-heldout2-lever-csc",
    ats: "lever",
    url: "https://jobs.lever.co/cscgeneration-2/3a04b45f-a2eb-438a-a8ac-b07324223813/apply",
    expect: {
      "label:Full name": "Maya Tremblay",
      "label:Email": "maya.tremblay@example.com",
      "label:Phone": PHONE,
      "label:Current location": { re: "^(Toronto, ON, CAN)?$" },
      "label:Current company": "Kinaxis",
      "label:LinkedIn URL": "https://www.linkedin.com/in/maya-tremblay",
      // "Where do you live? (City and State/Province)": the city alone is a partial answer.
      "name=cards[e5ea5c10-06d8-4dca-b744-4790b20a37c7][field0]": { re: "^(Toronto(, (ON|Ontario))?)?$" },
      // The page's label no longer names this box to the dump (2026-10-17): by its name.
      "name=cards[e86292bd-6ffb-499f-ab4b-4fbd9b9c65f4][field0]": "https://www.linkedin.com/in/maya-tremblay",
      "label:GenderSelect ...MaleFemaleDecline to self-identify": "Decline to self-identify",
      "label:RaceSelect ...Hispanic or LatinoWhite (Not Hispanic or Latin": "Decline to self-identify",
      "label:Veteran statusSelect ...I am a veteranI am not a veteranDecl": "Decline to self-identify",
      "radio=cards[64e9d670-076e-4974-b961-7f34c592bfcf][field0]": "Yes",
    },
  }),

];
