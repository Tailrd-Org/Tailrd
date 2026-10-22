# Round 8: SmartRecruiters' screening step on thirteen live postings, Greenhouse's lazy education lists, the remix multi-selects, 47 new boards, and banks for seven smaller ATSs (2026-10-22)

Branch `night/autofill-round8` (worktree `.worktrees/autofill-round8`), from
origin/main at 9cb84b0 (round 7). **Not pushed, not deployed**: nobody asked
this round. Nothing uploaded, no release zip built. Nothing was submitted
anywhere and no account was created: every live run blocks non-GET requests
(GraphQL queries aside), and every check of a posting was a GET.

The biggest finds:

- **What held round 7's ten SmartRecruiters postings on page 1 is their
  required résumé**, and a run cannot satisfy it: the extension now finds and
  attaches the résumé (it never did on SmartRecruiters: the file input sits in
  a shadow root), and the page then uploads it by a POST, which a run blocks.
  Only three of prod's 26 SmartRecruiters companies take a posting with
  nothing to upload (ServiceNow, Intuitive, Western Digital); thirteen of
  their postings were taken to the screening step live and every write read.
  On the way: every SmartRecruiters dropdown option read as its own text twice
  ("WA WA", through its tooltip), so Intuitive's state and Western Digital's
  education never stuck.
- **Greenhouse's Degree under load** was the driver typing into a list that
  loads only when a real click opens it. It now opens the list with a whole
  click first: 13 of 13 boards in a batch, GCM's education in 6 s (was 19 to
  26 s).
- **The self-ID widget had its own matcher**: on Gusto's live race list it
  took "East Asian (inclusive of …)" for a stated "Asian". Self-ID lists are
  now picked by the self-ID rules everywhere (scan, re-ask, widget, read-back).
- **The live regression caught six of this round's own regressions**
  (Palantir's age, Rippling's pronouns on three postings, Datalab's signature,
  Horizon's source) **and an older race** (Anduril's company, read before the
  page named it, so "Company website" took "Other"): each fixed with a
  failing test first and re-run live.
- **Seven smaller ATSs serve their forms to a plain GET**, so each got a
  question bank, read blind (JazzHR, Breezy, Pinpoint, Paylocity, BambooHR,
  Comeet, Recruitee: 255 postings, 2,928 questions). About forty wrong answers
  fixed, among them JazzHR's attestation that withdrew three applications,
  JazzHR's own citizenship list taking "Other" for citizens, "Lesbian" for a
  gay man, "Asian And White" for Asian applicants, a current salary written
  as the desired one, and a Bengaluru street ("14, 5th Cross, Indiranagar")
  dropped whole from every address box, found on a live check of the fixes.
  Re-running every bank after them caught five of their own regressions,
  fixed before anything was pinned.
- **The final full regression**, after the banks: 321 of 333 pinned cases
  pass. Of the other twelve, three postings had closed and one question had
  left its form. Two new writes were wrong (a year in Waymo's new month box,
  Scopely's "Yes" to relocation support), and reading the cases turned up
  two more: graduates' finished degrees on Tenstorrent's "level of education
  pursuing", and a round-8 pin that gave a June 2026 graduate "August 2026 -
  December 2026" (DV Trading). All four fixed with a failing test
  first and re-run live; the rest were right (pinned) or pass alone
  (section 7).

## 1. Real users first

Prod (read-only SELECT, branch main), checked at the start and again at the
end of the round (2026-10-22): **no autofill report since 2026-10-03** (the
last, 15:15, the owner's, read in round 7). No field captures and no pending
questions at all; the latest feedback is from 08-14, the latest security
event 10-17, the latest application record 10-15. P0 had no real fill to turn
into a case.

Active postings in prod (not duplicates, 2026-10-21), by host: Workday 1,757,
LinkedIn 1,139, Greenhouse 1,108, Indeed 554, Lever 211, SmartRecruiters 180
(26 companies, Bosch 124), Ashby 146, iCIMS 83, JazzHR 31, Oracle Cloud 23,
Workable 21, Dayforce 17, Recruitee 10, Pinpoint 9, Breezy 8, ADP 8,
Eightfold 8, Gusto 8, Jobvite 7, Comeet 3.

## 2. Open from round 7

### a. SmartRecruiters' screening step: what holds it, and thirteen postings through it

**What holds page 1.** A probe on Wellmark (GET only) shows the message:
"Please attach your resume to complete this application". The one-click
context's `features.resumeRequired` is the company's default, not the
posting's (Bosch's says no résumé while its page asks for one); the posting's
own rule is its form configuration, which the page GETs
(`/oneclick-ui/api/company/<id>/publication/<uuid>/config`, `fieldSets`). The
bank builder now keeps those flags (332595d, d7004f1). Across prod's 26
companies, only ServiceNow, Intuitive and Western Digital take a posting with
nothing to upload; EVERSANA and LinkedIn require work experience instead,
everyone else a résumé.

**The résumé itself** was never attached on SmartRecruiters: the input and its
"Choose a file / or drop it here" live in `<spl-dropzone>`'s shadow root, the
"Resume" title outside it. Fixed (f5b19b6; the "Apply with resume" box, which
parses a résumé and overwrites the answers, is never taken for it). Live on
Wellmark with a résumé supplied, the extension attached it and the page then
tried `POST /oneclick-ui/api/company/<id>/attachment/resume`, which the run
blocked. So a posting that requires a résumé cannot be taken past page 1 by a
run, by design.

**Thirteen screening steps, live** (r8a-sr-* and round 7's three): ServiceNow
eight (associate, SWE, FDRP twice, finance twice, Moveworks, staff) plus
round 7's ML role, Intuitive two plus round 7's AI intern, Western Digital;
Boston Engineering's page 2 as well. Every write read; what was wrong is in
sections 4 and 5 (0388937, f5b19b6, ed04364). On the fixed build Intuitive's
two reach "Done. 2 steps filled … Review and submit". All 17 new cases are
pinned (page 1; the screening writes were read in the runs); Bosch's three,
Circle, EVERSANA and LinkedIn hold page 1 for the résumé or the experience
their form requires, as a person would.

The new SmartRecruiters bank (`smartrecruiters-r8scan`: 20 postings, 331
questions, six personas) was read blind; its wrong answers and blanks are in
sections 4 and 5.

### b. Greenhouse's Degree under batch load: root-caused and fixed

A debug batch of 13 boards (E2E_DEBUG=1, an instrumented build) showed the
page-world driver missing **every** education select (School, Degree,
Discipline): 0 options after 3.7 s, then the ARIA fallback raced the same lazy
list; GCM's Discipline lost ("Couldn't open", the listbox open and empty).
Measured on GCM's page (GET only): typing loads nothing, the component's own
`openMenu` loads nothing, a lone mousedown or a focus leaves it shut; focus
with pointer and mouse down, up and a click on the input (what a person's
click sends) loads the list in 100 to 400 ms, and typing then searches.

The driver now opens the list with that whole click, waits while it loads,
picks from it, and types only when the first page lacks the answer (9bfdd0a,
eb00b11; the test's mock list opens on a click only and failed on the first
attempt's lone mousedown). Not round 7's longer wait (41971b8): nothing is
typed into a closed list now. **Verified in a batch of 13 on the final
driver: 13/13 pass, the driver itself opens every list** ("opened 165ms
opts=10 hit=true"; Discipline 73 options), GCM's education 6.1 s (19 to 26 s
before). The same batch found Arcesium's Degree, a list of degree titles,
blank for "Master": fixed (1a4926d).

### c. Greenhouse's remix multi-selects: four more boards, live

Riot (languages: the page ends "French, English"), Canonical (nationality:
"British", after 75047ea), Samsara's source list ("LinkedIn", after 7360612),
and Gusto's race list with two personas: one exposed the widget's own matcher
("East Asian (inclusive of …)" for "Asian"), fixed in 8318d44, 76e59ac and
c2792c6; the other's answer is the one option that is it. Jane Street's and
DV Trading's forms are their own (a checkbox group and a custom pronouns
widget), not remix. All pinned.

### d. The two calls, decided

- Azul's "Which location are you applying for?": decision 93 (f4a0589,
  20a0e29).
- A family member's government employment (ServiceNow's "D."): decision 94,
  the applicant's to answer (f4a0589).

### e. D-61: left as decided

Not overridden. Its blanks this round: Western Digital's citizenship
questions for the H-1B and OPT personas, and the sanctioned-country
questions for personas whose stated status names no citizenship (the
German "German citizen (EU)" included). Their pins stay blank.

## 3. New ground

### Greenhouse: 13 of the 16 boards in no bank and no case

r8b-gh-*: Insomniac, Legend Biotech, Horizon, Clear Street, Allen, DriveWealth,
Scopely, Bracebridge, EviSmart, Trace3, Hear.com, Via, i360. Blind, every
write read, fixed with failing tests first (14803aa, 9fdc9ca), re-run, pinned.

### Ashby: 13 boards in no bank and no case

r8c-ashby-*: 1Password, Amplify, Pebl, Aurora, Handshake, Jane, Illumio,
Patreon, AgZen, SpruceID, Metamorphic, Commure, Regent (c019a60). A new bank of
the boards not in the old one (`ashby-r8`: 34 postings, 385 questions) was
read blind too (a785e3f, 1773b24, 9f8368d, 69f9503, 4ebb67c, 3b3b571).

### Lever

Deep Genomics, Significance, and the boards new since 10-20 (Humble
Robotics, Stand Together): r8d-lever-*, pinned (ec66b27, 76e59ac).

### The smaller ATSs: 13 blind, 10 kept

Fewest cases first: Gusto 2 (its posting page links to `/applicants/new`,
no sign-in), Comeet 2 (its form opens in a frame of its own: bb52183),
Dayforce 2, Pinpoint 2, ADP 2, Recruitee 1, Eightfold 2. Dropped: Eightfold's
two (its apply page loads its questions by a POST that carries the answers
so far: decision 100) and ADP's Community Care Plan (its Apply opened
nothing live: a cookie overlay, then a bot-check POST). Oracle Cloud's form
sits behind an emailed PIN: skipped. JazzHR and Breezy already had the most
cases and were not reached within the 13. Fixed: Dayforce's "Not Completed"
box, Pinpoint's "happy to work from our office" (9001f2d).

**Banks.** Seven of them serve their forms to a plain GET, so each got a
question bank (`tools/qbank-<ats>.mjs`), read blind for six personas, every
answer; each wrong answer and answerable blank fixed with a test on the scan
path that failed first, and the bank re-run with every changed answer read:

| ATS | where the form is, by GET | postings (companies) | questions | commit |
| --- | --- | --- | --- | --- |
| JazzHR | each apply page as served (the job list names the jobs) | 86 (23) | 1,433 | 12ed805 |
| Breezy | each apply page's own data: the newer template's portal-data, the older one's attributes | 43 (7) | 519 | 49de933 |
| Pinpoint | the application page (`/applications/new`): native fields, and its questions (yes/no ones too) and equality lists configured in JSON scripts | 31 (4) | 516 | 5c3b84f |
| Paylocity | the apply page's `window.pageData`: the screener (each question's kind and answers in XML) and the included fields | 19 (4) | 145 | e26d10d |
| BambooHR | each job's detail JSON (`/careers/<id>/detail`) | 33 (6) | 183 | fac0d46 |
| Comeet | the hosted page's `QUESTIONNAIRES_DATA` | 15 (2) | 41 | 26b2a70 |
| Recruitee | its public offers API | 28 (1) | 91 | cdd0f19 |

Recruitee and Comeet had nothing wrong. What the others had is in sections 4
and 5. No bank for Dayforce (its questionnaire loads only inside the apply
flow; the posting page's data has none), Gusto (a script's GET is refused
with 403), Eightfold (a POST: decision 100), ADP (a bot-check POST) or
Oracle Cloud (an emailed PIN). Pinpoint's postings JSON names its postings
and places; its application page has the form.

**The fixes, live.** Thirteen live pages of these ATSs (four JazzHR, two
Breezy, three Pinpoint, two Paylocity, two BambooHR: `r8g-*`) were run on
the build with every bank fix, each write read, and pinned (954f6e9): among
them JazzHR's status list (decision 107), Infinitive's sponsorship, SDL's
attestation and its "April 2027" semester, Utah State's No, SimIS's PhD No,
Breezy's City and State abroad and a graduate's blank school, Pinpoint's
"studying", interview, bond and boolean answers with the current salary
blank, Paylocity's first page and BambooHR's yes or no in a text box. The
check found the Bengaluru street (section 4). What a run cannot reach is in
section 9.

LinkedIn and Indeed apply behind their own sign-in: skipped, as asked.

## 4. Wrong answers fixed

Live pages (the value written, and now):

| page | question | wrote | now |
| --- | --- | --- | --- |
| SmartRecruiters, every dropdown (Intuitive's state, Western Digital's highest level) | each option | read as its text twice ("WA WA"): "Selection didn't stick" | read once; picked |
| SmartRecruiters ServiceNow | a major list with "Electrical/Electronics Engineering" beside "Engineering" | "Engineering", for an Electrical Engineering degree | "Electrical/Electronics Engineering" |
| SmartRecruiters Western Digital | major (a multi-select) | "Selection didn't stick" while the page held it | read from its tag: filled |
| Greenhouse Legend Biotech | "Are you open to relocation?" | Yes, for an applicant who never said | blank (decision 49) |
| Greenhouse Bracebridge | "Are you currently attending Northeastern University?" | Yes, for a Northeastern graduate | No |
| Greenhouse Bracebridge | "Voluntary Equal Employment Opportunity Questionnaire:" over gender options | the decline, for a stated Female | "Female" (decision 90) |
| Greenhouse Gusto | race (remix multi-select) | "East Asian (inclusive of …)", for a stated "Asian" | blank, the applicant's pick (decision 99) |
| Greenhouse Gusto | sexual orientation | reported reverted while the page held it | recorded as filled |
| Greenhouse Insomniac | veteran status | blank in the final run (both "not a protected veteran" statements misread) | "I do not fall into one of the above categories of “protected veteran”" |
| Ashby Jane | "If you selected Other, please describe" follow-ups | "Non-binary", "Two or More Races" | blank |
| Ashby 1Password | "… Other than your ethnicity, gender, and disability status (survey below), is there anything you would like to share…?" | a self-ID answer | blank |
| Ashby Regent | "Do you accept the position at the salary & terms stated, if offered?" | Yes (read as consent to terms) | blank, the applicant's (decision 97) |
| Comeet Vast, Pinnacle | the whole form (a frame opened by Apply) | "No application form found on this page" | adopted and filled |
| Greenhouse Jane Street | "Pronouns (Select one or more.)": options above a self-describe box | "She/Her" typed into the box, which checks "self-describe" | the "she/her/hers" option, clicked (an adapter); read back from the checked options, as the page submits them |
| Greenhouse Anduril (regression) | "How did you hear about Anduril?", a stated company website | "Other" in one run of five: the page's company, named only in its title, was read before the title was there, and never again | "Anduril Website": a company still unknown is read again on each scan |
| Greenhouse Epic Games, Sigma (regression) | Degree, a bootcamp certificate's row | "Other" written (decision 96), then logged reverted and asked about again | recorded as filled |
| BambooHR Profound Medical (live, the banks' check) | "Address *" for a Bengaluru applicant ("14, 5th Cross, Indiranagar") | blank: its last part, the neighbourhood, read as a city took the street with it (any address without a civic number up front) | the street, whole |

Question banks (each also a unit test):

| company | question | wrote | now |
| --- | --- | --- | --- |
| 1Password (Ashby) | "What brought you to this job posting" (stated social media) | "LinkedIn Social Post" | "Other" (decision 9) |
| Commure (Ashby) | "How did you hear about Commure?" (a referral, social media) | "Other Job Boards" | blank: not offered, and the list has no Other (decision 102) |
| Clearco (Ashby) | "Are you legally able to work in Canada or the US without sponsorship?" | No, for a Canadian citizen | Yes |
| Heron Power (Ashby) | "How many years of full-time industry experience…? Do not include time from co-ops, internships…" | "1", from a student's co-op | "0" |
| Commure (Ashby) | "I am comfortable with the fact that this position requires being in the Mountain View, CA office at least 3 days per week." | Yes, for Seattle and Denver applicants who will not move | No |
| Switchboard (Ashby) | "What is your current work authorization status?" | the sponsorship statement, for an OPT holder | "I am currently authorized to work under OPT/CPT" |
| Samsara (Greenhouse) | "Where have you learned about Samsara?" (stated "Company website") | "Other" | "Samsara blog or website" |
| SoFi (Greenhouse) | "Today's Date of Application (MM/DD/YY Format)" | "10/20/2026" | "10/22/26" |
| GDMSI (SmartRecruiters) | "Please state the country you were born in:" (200 countries) | "Other", for three applicants | blank: no profile holds it |
| Sopra Steria (SmartRecruiters) | "Adresse (Straße, Hausnummer, Postleitzahl & Stadt)" | the street alone | the whole address |
| Western Digital, Resultant (SmartRecruiters) | "What is your major?", "What is your current or most recent major?" | "Computer Engineering", for "Computer Science and Engineering" | "Computer Science/Software Engineering", "Computer Science & Programming" (decision 104) |
| Chime, Coveo, Gusto, Salesloft, Samsara (Greenhouse) | race and ethnicity lists | "East Asian (inclusive of …)", for a stated "Asian" | blank (decision 99) |
| City and County of San Francisco (SmartRecruiters) | "If you selected Asian from the list above, please indicate whether your ethnicity is Filipino…" | "Asian - Non-Filipino", which no profile states | kept from everyone |

The smaller ATSs' banks (section 3; each also a unit test on the ATS's own
markup or data):

| company | question | wrote | now |
| --- | --- | --- | --- |
| every JazzHR page (86) | the job's place; the company | none on any page; "brand" (its logo's alt) | the job attribute titled Location; the title's company ("… - Career Page") |
| JazzHR's own field | "What's your citizenship / employment eligibility?" | "Other" for a US and a Canadian citizen | the applicant's own status option (decision 107) |
| Infinitive (JazzHR) | "Are you eligible to be hired by Infinitive … without the need for Infinitive visa sponsorship at any time…?" | inverted, all six | the right without sponsorship |
| Infinitive (JazzHR) | "Is your work visa currently being sponsored by an employer?" | Yes (the need's) for three who hold no visa | what the visa is: No without one, Yes for an employer's |
| Infinitive (JazzHR) | "Are you currently legally authorized to work in the US for any employer?" | Yes for an H-1B | blank (its statement is withheld, D-61) |
| JazzHR | a misrepresentation attestation ending on the employer's disability policy | "I don't accept these terms…": read as self-ID, it withdrew three applications | its consent option |
| JazzHR | "Are you currently enrolled at Utah State University?" | Yes for a Waterloo student | No |
| JazzHR | "Do you have a bachelor's or graduate degree in electrical or computer engineering?", an MS in physics | Yes for an MS in Data Science or Information Systems | No for a degree in another field |
| SimIS (JazzHR) | "Do you have the desired • PhD in physics … with six (6) years of experience?" | Yes for 14 years and no PhD | No |
| JazzHR | "…at least 2 years of professional experience working as an Appian Developer", "How many years of experience do you have as a Data Engineer?" | a staff engineer's 14 years | No or the lowest range from a short career, else blank (decision 106) |
| JazzHR | "Please select your Japanese language proficiency level" | "Native", for six profiles listing no Japanese | the list's "none" option (decision 105) |
| JazzHR | "Which language are you applying for?" | every language spoken | blank: theirs to name |
| JazzHR | a graduate degree's field; a BBA's field; "College or University" | a bachelor's, bootcamp's or student's field; "Administration"; a bootcamp | blank; "Business Administration"; blank |
| JazzHR | "two forms of valid ID" | Yes, from the driver's licence | blank |
| Foxconn (JazzHR) | "If hired, can you show proof that you are eligible to work in the U.S.?" and a sponsorship follow-up, in text boxes | the status statement ("Canadian citizen") | Yes or No |
| JazzHR | "Completion date of your highest degree (mm/yyyy)" | the degree's name | its month and year |
| Gastronomous (Breezy) | "What school are you enrolled at?" | five graduates' old schools | the school of a degree in progress, else blank |
| VetsEZ (Breezy) | "Are you willing to undergo a security clearance process?" | No for everyone (read as holding one) | Yes (decision 49) |
| JND (Breezy) | "…communicate fluently in any language other than English?" | a conversational Hindi over a native Tamil | the one language stated at a fluent level that the list names, else "Other"; none at that level, "No" |
| Zendar (Breezy) | "What makes you a good fit for this position? What unique skill…" | the skills list | blank |
| JND (Breezy) | "(Optional) Please provide details if there are any conditions or restrictions for employment…" (described "e.g., VISA Sponsorship…") | the profile's sponsorship "No" | blank: the applicant's own details |
| Franklin Electric (Pinpoint) | "What Franklin Electric location do you currently work from?" | the applicant's home city | blank: it asks the employer's own staff |
| Franklin Electric (Pinpoint) | "What is your current Annual Salary (CTC)?" | the desired salary | blank: the pay now is never given |
| White Swan (Pinpoint) | "Sexual Orientation" [Heterosexual / Bisexual / Homosexual / Lesbian] | "Lesbian", for a gay man | "Homosexual" (decision 110) |
| White Swan (Pinpoint) | the UK's ethnicity list (White British … Asian And White … Pakistani) | "Asian And White" for two Asian applicants; "Black African" for an African American | blank (decision 111) |
| Impulse Space (Pinpoint) | "Are you legally authorized to work in the United States as per the below ITAR requirements?" (a yes/no the first bank build missed: Pinpoint's yes/no components name no field) | Yes for the H-1B and OPT holders | blank, kept from the AI: a visa holder may yet obtain an export authorization (as decision 62); Yes for a citizen, No with no US work right |
| RF-SMART (Greenhouse), Terranova (Ashby), SDL (JazzHR) | "What is your intended semester and year of graduation?", "Graduation Semester", "…(please include year & semester)?" | the year alone ("2014" too) | the month and year ("April 2027"); nothing from a year alone (decision 115) |
| Jane Street (Greenhouse bank) | "Extra", a long-text box described "Why you're interested in Jane Street How you heard about us…" | the street, for five personas (read as an address by "Street" in the company's name; live the box stays blank) | blank: a long-text box takes a street only when its label asks for an address |
| Riot, FiscalNote, Crest, Sopra Steria (banks) | a whole address, for a Bengaluru applicant | "Bengaluru, Karnataka, India" (no street, the parsing bug in section 4's live table); then without its state | "14, 5th Cross, Indiranagar, Bengaluru, Karnataka 560038, India": a state the region table lacks, in the profile's words |
| Choice Solutions (Paylocity) | "What are your salary expectations for a role like this?" [$40-45k / … / $55k+], a box each | nothing ticked: "k" unread, and "$185,000" split on its thousands comma | "$55k+" |
| ATB (Paylocity) | "What is your expected graduation date? (Month/Year)" | "2014", "2022", "2011": years alone | blank (decision 73) |
| Lightology (Paylocity) | "How many years of experience do you have with backend web development languages such as PHP, JavaScript?" | "Less than 1 year", from a short career | blank: a skill's years (decision 106 caps a role's) |
| Solace (BambooHR) | "This position would be 2 days in-office in the Ottawa area, is this something that will work for you?" in a text box (Singapore's too) | the current job title | Yes or No, by where the applicant lives and will move |
| Picton Mahoney (BambooHR) | "How many years experience fo you have in the financial services industry?" | the career total | blank |
| Profound Medical (BambooHR) | "Have you managed end-to-end event logistics including … travel, and onsite execution?" | No, for two who will not relocate | blank: experience, no requirement |

Caught mid-round by the bank re-runs (this round's own rules), each fixed
with a failing test before the next run: Webflow's "White" gone blank and
Hootsuite's decline answering "Yes, I have a disability…" (the self-ID snap
reorder: 4752d62); Barnes & Thornburg's "Law Degree" and Bosch's "current
degree that you are obtaining" taking "Other" (decision 96 overreaching:
4752d62, d58c826); Kepler's country list gone blank and Woven by Toyota's
background-check acknowledgment kept from everyone (decisions 93 and 97
overreaching: 20a0e29); Jane's two-country heading answered for the job's
country (a785e3f); and the source-list fix's brands in brackets (Airbnb,
Brex, D2L, Flexport, Lucid, Mixpanel, Waymo), Fivetran's "Referred", Planet's
"Other - Webinar", Vercel's two referral options, Klaviyo's five social media
options and Cloudflare's colon (d67c93a, 686bcd7); CTC's "State (If N/A,
Select Other)" losing its "Other" when "please state" became a verb
(695fca9).

The re-run of every bank after the smaller ATSs' fixes caught five of their
own regressions, each fixed with a failing test first
(`test/bankRerunRound8.test.ts`) and the banks re-run: Appian's "Are you
currently pursuing a Major in one of the following disciplines: … or a
related STEM field" was Yes for four graduates (the new STEM rule counted any
degree); Pinterest's "please describe your PhD program" took two master's
fields; Instacart's "Black or African-American (A person of African,
Caribbean, West Indian descent)" read as a mixed option by its description's
"Indian"; NISC's "k" list read "€120.000" as 120 ("less than 25k"); and
Credence's "How did you hear about us? Was it a job board or an employee?"
became a yes or no and lost every answer.

Caught by the live regression (section 7), each fixed with a failing test
first and re-run live:

| page | question | regressed to | cause | now |
| --- | --- | --- | --- | --- |
| Lever Palantir | "Give us three numbers that describe you … We're not looking for your predicted grades or your age …" (a textarea) | "22", the applicant's age | its real question became its label (76e59ac) and read as an age question | blank (e53ea82) |
| Rippling Fluid AI, 4AG (three cases) | Pronouns: She/her/hers, He/him/his, … , "She/her/hers, they/them/theirs" | blank (and the failed pick shifted the phone code's element) | the widget picks self-ID by the self-ID rules (8318d44), where two options hold "she her" | the option of the stated set (146fc7b) |
| Lever Datalab | "By entering your name below: I, the undersigned, certify … criminal background and drug check." | blank (round 7: the name, read by the card's "SIGNATURE" heading) | read by its own question (76e59ac): no name field, and "criminal" made it unanswerable | the full name (801e130) |
| Greenhouse Horizon | "How did you hear about us?" (eleven "…-jobsearch.com" boards, "Other Jobboards", …), a stated LinkedIn | blank | "Other …" channels stopped standing for "Other" (1773b24), and "Jobboards" in one word was no job board | "Other Jobboards" (503387c) |

Caught by the final regression (section 7), each fixed with a failing test
first (3fbfdd3) and re-run live:

| page | question | wrote | cause | now |
| --- | --- | --- | --- | --- |
| Waymo (Greenhouse's education block on Waymo's own careers site, new on the page since the morning's regression) | "End date": a number box "MM" (1 to 12) beside a "YYYY" box | "2027" in both | the block's ids carry the question set's number, read as row 4; once the rows were renumbered, both boxes were resolved again as a bare graduation year | "4" and "2027" for an April 2027 graduation; a year alone leaves the month blank |
| Tenstorrent (Greenhouse) | "Current level of education pursuing" | live, a student's "Bachelor's Degree" (right); on the posting's own options (GET), every graduate's finished degree ("Master's Degree", "High School") | "pursuing" counted a finished degree, a reading meant for Axtria's "Highest Degree Earned/Pursuing" (the JazzHR bank) | the degree in progress; blank for a graduate, kept from the AI |
| Scopely (Greenhouse) | "Will you require relocation support?" [Yes – I would be relocating for this role. (… would need relocation support.) / No – I am already based in the required location. / No - I am not willing to relocate.] | the "Yes", for anyone who would move | the Breezy fix that chooses location options first ran before the guard that keeps help with a move the applicant's (the day session's decision 2) | blank; "No - I am not willing to relocate." for those who said so |
| DV Trading (Greenhouse, pinned in round 8) | "Please re-confirm your expected graduation date" [I've already graduated / August 2026 - December 2026 / …], a June 2026 graduate | "August 2026 - December 2026" | the list loads only when opened, so the write matched "June 2026" by its words, and the option's two "2026"s counted as two shared words of four | each word counted once: no match, and the list's real options go back to the resolver: "I've already graduated" |

## 5. Blanks now answered

| page | question | now |
| --- | --- | --- |
| SmartRecruiters ServiceNow (live) | "What school do you currently attend or most recently attended? If unlisted, please select "Other"" | "Other" for a school the list lacks (decision 95) |
| SmartRecruiters ServiceNow (live) | "Following the completion of your internship, will you be returning to school…?" | No for a graduate |
| SmartRecruiters ServiceNow (live) | a German Diplom-Informatiker's major | Computer Science |
| SmartRecruiters Intuitive (live) | "If you live in one of these states, would you consider relocation?" | its N/A option, for someone living in none |
| SmartRecruiters Western Digital (live) | "Do you have a Family or Close Friend relationship with anyone employed by WD?" | No (the relatives default) |
| SmartRecruiters (live) | available "during the work period specified in the … posting" | Yes (decision 49) |
| SmartRecruiters Boston Engineering (live) | "This position may require security clearance. Can this be achieved?" | No for an F-1 OPT holder (decision 81): the job's place is now read from the page's top bar |
| Greenhouse Arcesium (live) | Degree, a list of degree titles | the row's own title ("Master of Science", "Bachelor of Engineering") |
| Greenhouse Hear.com (live) | Degree, for "Baccalauréat en psychologie" and a certificate row | "Bachelor's Degree"; the certificate the list's "Other" (decision 96) |
| Greenhouse Bracebridge (live) | "Are you in pursuit of a MS degree … at Northeastern University?" | No for someone studying elsewhere |
| Greenhouse Trace3 (live) | "What are your current salary requirements?" | the stated expectation |
| Greenhouse Canonical (live) | "Please indicate your nationality:" (remix multi-select) | "British" |
| Greenhouse Samsara (live) | "Where have you learned about Samsara? Select all that apply." | "LinkedIn" |
| Greenhouse Gusto (live) | "Which metro area are you located in?" | "Denver"; "Other" for a Boston applicant (decision 103) |
| Ashby 1Password (live) | "Are you a former 1Password employee?"; "What brought you to this job posting" | No; "LinkedIn Jobs" |
| Ashby Amplify (live) | "employed with Amplify", "currently working with Amplify Education, Inc.", a school district or state education agency | No |
| Ashby Pebl (live) | "engaged with Pebl … as an employee"; a start the posting sets; its full duration | No; by the stated earliest start; Yes (decision 86) |
| Ashby Jane (live) | "Work Eligibility in the United States" (a heading over Yes and No) | the work right |
| Ashby Regent (live) | "Available to start immediately? If not, when?" | Yes within a week of the stated start (decision 98) |
| Lever Significance (live) | "Are you local to the DMV to accomodate a hybrid work schedule?" | No for an Austin applicant (decision 101) |
| Lever Stand Together (live) | the Koch Internship Program question (470 characters) | read as the select's question (it took the card's heading) |
| Dayforce Woodmen (live) | "Not Completed" (an education row) | ticked while the degree is in progress |
| Pinpoint White Swan (live) | "Are you happy to work from our New York office 5 days a week?" | from where the applicant lives and will move |
| AnySignal (Ashby bank) | ITAR's "I am currently a 'U.S. Person'" | for a citizen or permanent resident |
| 1Password (Ashby bank) | military capacities ("No", "Active Duty", …) | "No" for someone who never served |
| Aurora (Ashby bank) | "I identify as LGBTQIA+" / "I do not identify as LGBTQIA+" | by the stated orientation or identity |
| AssortHealth (Ashby bank) | "What type of visa will you require? (if applicable to above)" | "Not applicable" for someone who needs none (decision 92) |
| Switchboard (Ashby bank) | "What is your current work authorization status?" | "…without restrictions" for a US citizen |
| Wellmark (SmartRecruiters bank) | "Please provide detail regarding any visa and/or employer-based sponsorship requirements" | "None of these apply…" for someone who needs none |
| LinkedIn (SmartRecruiters bank) | "Please select your degree type" [BA/BS, Masters, PhD, N/A] | "BA/BS" for a bachelor's |
| Geotab, New Relic, Tubi (Greenhouse banks) | "Were you previously employed with Geotab?", "Are you currently working at New Relic?", "…employed with Tubi…?" | No, for a history without them |
| CircleCI (Greenhouse bank 3) | "Are you a veteran?" [I identify as one or more… / I do not identify as a protected veteran / …] | "I do not identify as a protected veteran" |
| JazzHR banks | "Graduate Degree or higher", "Bachelor's Degree or Higher" (a master's); "Highest Degree Earned/Pursuing" (a student's degree in progress) | the level held |
| JazzHR banks | "18 years or older"; "willing to move to New Jersey area if you reside elsewhere"; relocation options written as sentences | the age; the stated relocation |
| Space Dynamics Lab (JazzHR) | how you heard, a stated company website | "Space Dynamics Lab Website" |
| JazzHR banks | "clearable" | read as able to obtain one (decision 81) |
| Zendar (Breezy) | its on-site acknowledgment, two Nos ("…remote-only job", "…not able to relocate") | the No about relocating |
| Breezy banks | "Did you major in STEM?"; "(City, State)" outside North America | by the major; "Bengaluru, Karnataka" (a city that is its own state once) |
| Franklin Electric (Pinpoint) | "Are you currently studying?" | Yes for a student, else No |
| Franklin Electric (Pinpoint) | "Have you ever been interviewed by … Franklin Electric earlier?"; "Do you have contract/bond agreement with the present employer?" | No (the prior-application default; decision 108) |
| Franklin Electric, White Swan, Flatirons (Pinpoint) | Gender [… / No Response]; Veteran Status with "I Choose Not To Identify" beside "I Am A Protected Veteran, But I Choose Not To Self-Identify…" | the decline, for a profile with no EEO answers (decision 109) |
| Lightology (Paylocity) | "Are you residing in any of the following states: AL, CO, CT, …, and WY?" | by the home state |
| ATB (Paylocity) | "What is your current academic status?" [Undergraduate – Freshman … / Technical, Trade, Vocational, or Certification Program (Completed)] | "Undergraduate – Senior" for a final-year student; the completed certification program for a bootcamp graduate (decision 112) |
| Profound Medical (BambooHR) | "Highest Education Obtained", a B.Tech | "College - Bachelor of Science" (decision 114) |
| Solace (BambooHR) | "Are you currently eligible to work in the European Union for any employer?" | Yes for a stated EU citizenship (decision 113); the personas' "German citizen (EU)" gives none under D-61 |

## 6. Decisions (one rule each, each easy to reverse)

93. "Which location are you applying for?" takes the applicant's own city (its
    metro) when the posting lists it, else the remote option of the
    applicant's own country, else (a list of countries) their own country;
    otherwise blank, and not the AI's to guess. Round 7 took Azul's remote
    option only when the address happened to name its country.
94. A family member's government employment is the applicant's to answer:
    never the relatives default's No, never the AI's. Public employment is
    common (teachers, nurses, postal workers); a relative who is a government
    official stays decision 85's question.
95. A school list that says "If unlisted, please select Other" takes "Other"
    for a school it does not carry.
96. An education row whose degree no level names (a bootcamp's or a
    professional certificate) takes the row's Degree list's "Other"; a degree
    asked at one level (law, undergraduate, graduate) or as the one being
    obtained, for a finished row, stays blank.
97. Accepting a position at the stated salary and terms, if offered, is the
    applicant's decision, never the AI's; only a question asking whether they
    accept is it (a notice that mentions an offer is not).
98. "Available to start immediately?" is Yes when the stated earliest start
    is within a week, else No; a legal right to start stays work
    authorization.
99. A stated self-ID answer that several options narrow ("Asian" among East,
    South and Southeast Asian) is no pick: left to the applicant. The option
    that starts with the answer is it; a decline never answers Yes or No.
100. A form that loads its questions by a POST carrying the answers so far
     (Eightfold) is not run live: the rule blocks the POST, so it never
     renders. Its fills need a person.
101. The DMV is Washington, DC and its Maryland and Virginia suburbs: "local
     to the DMV" is Yes from DC, No from any other state or country, and the
     applicant's inside Maryland or Virginia.
102. Source lists: "Social media" is no particular platform's (decision 9);
     a channel the list offers more than once (two referral options, five
     social media ones) is the applicant's pick, never "Other"; the list's own
     Other is "Other" alone, bracketed or asking to specify ("Other Job
     Boards", "Other - Webinar" are channels); a referral the list has no
     referral or employee option for is "Word of mouth".
103. "Which metro area are you located in?" takes the applicant's own metro,
     and "Other" only when every place listed is known to be elsewhere.
104. A major of two fields joined by "and" ("Computer Science and
     Engineering") is its first field, unless the list has it whole.
105. A proficiency level asked for a language the profile does not list is
     the list's "none" option; never another language's level.
106. Total years cap a role's years but never prove them: under a count the
     whole career is under, No (or the lowest range); otherwise the role's
     own rows when every title names it, else blank. A skill's years
     (languages, tools) are no role's: blank.
107. JazzHR's citizenship / employment eligibility list takes the applicant's
     own status (a citizen's or permanent resident's country), else, with no
     right to work in the job's country, "Non-citizen seeking work
     authorization"; never "Other".
108. The non-compete default (No) covers an employment bond (an agreement to
     stay or repay) with the present employer.
109. "No Response" declines; of several declines, the first listed; an
     option that claims a status before declining its details ("I Am A
     Protected Veteran, But I Choose Not To Self-Identify…") is that status,
     never a decline.
110. "Gay or Lesbian" is "Homosexual" where the list has it, else "Gay";
     never "Lesbian" alone (it turns on a gender no list asks); with both
     listed, the applicant's pick.
111. One stated race is never a mixed option ("Asian And White"); a list
     that splits that race by origin (White British / White Irish; Black
     British / African / Caribbean) is the applicant's pick.
112. Academic status for someone enrolled nowhere whose highest education
     is a finished certificate program: the list's completed certification
     option (decision 75 otherwise).
113. The European Union (or the EEA), asked as one place: a citizen of an
     EU or EEA country, or Switzerland, may work there; anyone else's right
     there is unknown, and blank.
114. On a list of a level's kinds (Arts, Fine Arts, Science), an
     engineering, technology or applied science degree is the Science one.
115. A graduation semester asked in a box is the month and year ("April
     2027"): it names the semester whatever the school calls its terms; a
     year alone gives nothing (decision 73).

## 7. Regression

**Question banks.** Every bank re-run on the final answer code against
round 7's final run (the base), every changed answer read:

| bank | answers | changed since the base | in questions | what changed |
| --- | --- | --- | --- | --- |
| Greenhouse 1 | 11,256 | 64 | 22 | dates (the runs are two days apart), race lists (decision 99), Canonical's nationality, Samsara's source, Gusto's metro, SoFi's date format, Geotab's former employee, Astranis' and Brex's labels read as no State (same answers) |
| Greenhouse 2 | 10,068 | 29 | 14 | dates, sources (decisions 9, 102), Tubi's employee, a government question naming family (decision 94), NISC's locations kept from the AI (decision 93), a Diplom's field, Cloudflare's return to school after an internship (blank for a student: when the internship falls is unknown) |
| Greenhouse 3 | 9,528 | 37 | 15 | dates, CircleCI's veteran, New Relic's employee, Geotab, NISC, race lists, Hootsuite's disability decline, CTC's work-authorization statement for a citizen, a Diplom's field, sources |
| Ashby | 4,908 | 5 | 5 | 3 dates; Quora's Degree ("Other" for a certificate, decision 96) and a Diplom's discipline |
| Lever | 6,732 | 142 | 14 | 8 "Which location are you applying for?" lists (decision 93), 5 start dates, Datalab's signature (each persona's full name, 801e130) |
| Workable | 4,926 | 66 | 7 | dates only |
| Jobvite | 5,952 | 13 | 3 | dates only |
| SmartRecruiters | 4,068 | 124 | 19 | 24 of "Today's date", ServiceNow's family government question (decision 94), schools and majors (decisions 95, 104), the born-in country, Sopra Steria's address, Wellmark's visa list, WD's relatives and work period |

New this round, read blind: Ashby's other boards (34 postings, 2,310
answers), SmartRecruiters' optional-résumé postings (20 postings, 1,986) and
Recruitee (28 offers, 546). The re-runs between fixes caught this round's own
regressions (section 4's last paragraph), each fixed with a failing test
before the next run; the last re-run changed only what its fixes meant to.
After the live regression's fixes, all eleven banks (62,280 answers) were
run once more: the only change is Datalab's signature, six personas' full
names.

**After the smaller ATSs' fixes** (JazzHR to BambooHR, section 3) and their
live check (section 4), all seventeen banks (79,302 answers; Pinpoint's
rebuilt with the 67 yes/no questions its first build skipped) were re-run on
the final answer code: the eleven against their run after the live
regression's fixes, each new bank against its own run after its fixes. Every
changed answer read. Five were this work's own regressions, and three more
wrong answers came to light (ITAR's work right, a graduation semester, Jane
Street's "Extra"): each fixed with a failing test first (section 4) and every
bank re-run, until a run changed only what its fixes meant to. The street fix
(section 4) shows wherever a bank has street boxes: "India's street" below.
Net change:

| bank | answers | changed | in questions | what changed |
| --- | --- | --- | --- | --- |
| Ashby | 4,908 | 24 | 9 | Clay's "Most Clay employees are in office 3+ days a week. Does that work for you?" now a yes or no (by where each lives and will move); Cobot's "a Bachelor's degree in one of the fields mentioned above" blank (the fields are not on the form; it was Yes for three); Terranova's "Graduation Semester" (decision 115); a BBA's major; a city and state abroad; "…for any employer" blank for the H-1B and OPT holders; India's street |
| Ashby (round 8 boards) | 2,310 | 2 | 2 | cities and states abroad ("Bengaluru, Karnataka") |
| Greenhouse 1 | 11,256 | 20 | 12 | Box's "City, State and Zip" ("Berlin 10119"); Lucid's supply chain years blank for a data analyst (decision 106); Pinterest's PhD program kept from the AI for those holding none; Salesloft's "1+ years of FP&A" No for a three-month career; Jane Street's "Extra" blank (it took four personas' streets); India's street and whole address |
| Greenhouse 2 | 10,068 | 35 | 15 | year-only graduations in "(month and year)" boxes, blank (decision 73); RF-SMART's semester (decision 115); DV Trading's privacy consent ticked; a BBA's discipline; Edmentum's "Do you have a PhD?" No for a Diplom; NISC's "k" compensation list by number (its place is not read, so no currency is known: a Canadian figure is taken as it stands, section 9); "…for any employer" (H-1B, OPT); India's street and whole address |
| Greenhouse 3 | 9,528 | 23 | 11 | Lattice's SaaS sales years ("0-2 years" from careers under two years, decision 106); the US NISC posting's compensation list; Man Group's STEM degree (Yes for five, the bootcamp blank); a year-only graduation (decision 73); India's street |
| Lever | 6,732 | 11 | 8 | Palantir's post-college years ("0" for a new graduate); Wintermute's IT support years ("Less than 1 year"); "…for any employer" (H-1B, OPT); India's street and whole address |
| Recruitee | 546 | 6 | 1 | Huawei's "Do you possess a Master or PhD degree in Security related field?": No for all six |
| SmartRecruiters (two banks) | 6,054 | 10 | 8 | Western Digital's highest level: "Bachelors of Science" for a B.Tech (decision 114); India's street and whole address (Sopra Steria, EVERSANA, CRB) |
| Workable | 4,926 | 33 | 8 | DISA's "mining, manufacturing, or industry related" years ("0-2 years", decision 106); United Placement's "4+ years of outside/In home Sales" No for short careers; Saalex's named-field bachelor's blank for a BBA (was Yes); India's street |
| Jobvite | 5,952 | 64 | 3 | India's street (Ross Video, ActioNet: line 1 filled, line 2 blank) |
| JazzHR | 8,598 | 99 | 5 | a B.Tech's highest level ("College - Bachelor of Science"); TUM under "College or University"; SDL's "5+ years … with a BS, 3+ … with a MS, or 0+ … with a PhD" blank where it was No (alternatives, not one degree); SDL's semester (decision 115); India's street |
| Breezy | 3,114 | 56 | 5 | JND's conditions-details box kept from the AI (blank, as before); Berlin's "(City, State)" "Berlin" (was "Berlin, Berlin"); Gastronomous's bare "Onsite" YES for the four who will relocate; India's street |
| Pinpoint | 3,096 | 402 new | 16 new | its 67 yes/no questions, read blind: one wrong, the ITAR question (fixed); the 2,694 answers before them unchanged |
| Paylocity, BambooHR, Comeet | 2,214 | 0 | 0 | |

After the final regression's fixes (section 4's last table, 3fbfdd3), all
seventeen banks were run once more and read: four answers changed, all on
Resultant's "What is your current or most recent major?" (SmartRecruiters):
"Other" for Mechatronics Engineering and for a back-end bootcamp, where the
repeated "Engineering" of "Engineering (Pre-Engineering), General" had taken
them. A major the list lacks is "Other", a sibling discipline is not (the
rule since Palantir's list, 2026-10-03).

**Live regression.** Every pinned case in batches of 13, each
SmartRecruiters case in a browser of its own, one run at a time.

First pass, on the build of 695fca9: 329 cases (round 7's 266 and this
round's 63), **294 passed**. Each of the other 35 was read:

| what | cases | done |
| --- | --- | --- |
| postings closed since round 7, each checked by GET: Justworks, Vestmark and Carvana's embed (Greenhouse's job API 404), Kenect (gone from its Breezy /json), SAS (iCIMS 410), AEP (its Workday job API 403 where an open AEP posting answers 200), Ross Video, SRC and Nutanix-1 (Jobvite sends them to its job list) | 9 | retired, one line each |
| this round's rules answering what was blank, every write read and right: Epic's and Sigma's certificate row "Other" (decision 96), Eclipse's "Not Completed" (a degree in progress), Flourish's, SSCI's and AMCA's field of study (a Diplom-Informatiker), Kobie's own city (decision 93), Point72's "I identify as" decline (decision 90), EQ Bank's two questions added since round 7 | 9 | pinned |
| Arc'teryx's "Will you remain enrolled … and return to your program following the co-op?": round 7's Yes was the requirement default; "following" now reads as "after", a student's own plan (decision 63) | 1 | pinned blank |
| this round's regressions: Palantir, Rippling (three), Datalab, Horizon | 6 | fixed, failing test first (section 4) |
| an older race: Anduril's company read before the page named it | 1 | fixed (section 4) |
| the harness read SmartRecruiters' page before its form rendered, so the page's own "+ 1" country code scored as a write (five), and ServiceNow FDRP's City and Website were still rendering | 6 | harness fixed (9d3f13f); each passes |
| Stand Together's location typeahead (a Cloudflare challenge mid-run); K1's how-you-heard question gone from its form; Recruitee's phone input renamed (React's own id) | 3 | passes alone; pin dropped; pinned by its label |

Every one re-run on the fixed builds: all pass (the thirteen re-pinned live in
one batch, 250 of 250 checks; the SmartRecruiters six one by one; Anduril
three runs of three, where one in five failed alone before).

Final pass, on the build of 8412980 (every fix above): 320 cases (the 329
less the nine retired), **317 passed**. The other three, each read:

| case | what happened | done |
| --- | --- | --- |
| Duolingo (Greenhouse) | the page never loaded: `page.goto` timed out at 60 s | passes alone, 28 of 28 |
| Azul (Lever) | Lever's location search listed nothing within the adapter's 12 s wait (the case took 17 s; 6 s alone), so the location was left for the applicant to pick, as designed; but its report said "failed" with no reason, because the adapter path dropped it | passes alone, 13 of 13; the adapter's reason now reaches the report (c054b16, a failing test first) |
| Wellmark (SmartRecruiters) | no form for more than 9 s, past the harness's wait, so the page's own "+ 1" country code scored as a write once more | when the form is still missing after the wait, the harness reads the page the moment before it presses Autofill (ffb341e): a local page on SmartRecruiters' host that renders its form 11 s late failed before the change and passes after; Wellmark passes alone, 6 of 6 |

Last pass, on the final build (ffb341e): 320 cases, **315 passed**. Each of
the other five passes alone:

| case | what happened | done |
| --- | --- | --- |
| Pure Storage (Greenhouse) | School blank: its pick missed under batch load (an 18.7 s local fill), and the re-ask, with no options read, went to the backend, which drops it while the AI is dead | passes alone, 25 of 25 (section 9) |
| Humble (Lever) | the location under batch load again (section 9); its report now says why: "Pick your location from Lever's suggestion list." (c054b16, live) | passes alone, 8 of 8 |
| Riot (Greenhouse) | for once the fill added the persona's second school (Concordia's certificate) and wrote its school, discipline and year, all the persona's, which read as UNEXPECTED; its Degree stayed blank (the list never loaded) | passes alone (no second row); the row is checked when it is there, its Degree "Other" (decision 96) or blank (766edc5) |
| Huawei (Recruitee) | the pinned "EN" is the captcha frame's own language picker, and that frame did not load | passes alone; checked when it is there (766edc5) |
| M3 (SmartRecruiters) | the fill started 6.5 s after load, mid-render (section 9): City and Website blank | passes alone, 8 of 8 |

Rescored against the new pins, Riot and Huawei pass with and without their
fields (Riot 37 of 37 and 32 of 32, Huawei 6 of 6 and 5 of 5).

**Final regression**, after the smaller ATSs' banks, on the build of 464465f
(pins 954f6e9): 333 cases (the 320 and the thirteen `r8g-*`), **321
passed**. Each of the other twelve, read:

| case | what happened | done |
| --- | --- | --- |
| CesiumAstro (Lever), NinjaHoldings (Breezy), York (Dayforce) | the postings closed: Lever's postings API answers 404; Breezy's page reads "Position Closed" and its position list lacks it; Dayforce's job data turned postingStatus 4 that day (its open postings read 1) | retired, one line each |
| K1 (JazzHR) | "Did/Do you attend university in Southern California…?" left the form (its apply page, GET) | checked only when it is there |
| Waymo, Scopely (Greenhouse) | new writes, both wrong: a year in Waymo's new month box, Scopely's "Yes" to relocation support (section 4's last table) | fixed, failing test first (3fbfdd3), re-pinned |
| Tenstorrent (Greenhouse) | a new write, right: a student's "Bachelor's Degree" for "Current level of education pursuing"; on the posting's options, graduates took their finished degree | pinned; the graduates fixed (3fbfdd3) |
| DV Trading (Greenhouse) | "Terms & Conditions", whose one option is the privacy consent, newly ticked (right: the day session's decision 3); reading the case, its round-8 pin was wrong: "August 2026 - December 2026" for a June 2026 graduate | the consent pinned; the graduation fixed (3fbfdd3) and re-pinned "I've already graduated" |
| GEI (Jobvite) | Address line 1 now "14, 5th Cross, Indiranagar" (was blank): the street fix, live on Jobvite | pinned |
| Humble (Lever) | the location under batch load (section 9) | passes alone, 8 of 8 |
| Bosch HR (SmartRecruiters) | clicked mid-render: labels still Lit's markers (section 9) | passes alone, 9 of 9 |
| Franklin Electric intern (Pinpoint) | "Have you previously been employed by Franklin Electric?", shown once "currently employed" is No, left blank (section 9) | passes alone, 16 of 16 |

Every case above that was not retired was re-run live on the final build
(3fbfdd3), and all pass. Waymo's run wrote "4" and "2027" and scores 25 of
25 against its new pins; since then its page has answered every load with
an AWS WAF challenge, whose POST a run blocks, so its form never renders. In
the same re-run Franklin Gujarat (Pinpoint) once filled nothing on its form
(section 9); alone it passes, 22 of 22. 330 cases are pinned now.

## 8. Verification

On the final code (3fbfdd3; after it come the pins, e126b83, and this
report):

- Extension unit tests: 3,183 pass (176 files; 1 skipped as before); `tsc
  --noEmit` clean; build; `test/scan-smoke.mjs` passed.
- Every fix in sections 4 and 5 has a test that failed first, on the scan
  path where a category could answer first, and was re-run live on its page
  where a run can reach it (section 9 names what it cannot).
- All seventeen question banks (79,302 answers) re-run on the final answer
  code, every changed answer read (section 7).
- The full live regression, after the banks, on the build of 464465f: 321 of
  333. The twelve others were read (section 7): three postings retired, the
  new writes fixed or pinned, and every case they touched re-run live on the
  final build, all passing. An adapter's reason reached a live report
  (Humble's location).
- The harness's read at the click (ffb341e) on a local page that renders its
  form 11 s after load: failed before the change, passes after.
- Not run: CI's backend pytest and frontend checks. Nothing outside
  `chrome-extension/` changed this round (NOTES.md aside), and nothing was
  pushed; before a push, run them as the brief says.

## 9. Needs manual verification, or still open

- **A required résumé on SmartRecruiters**: the extension now attaches it,
  but the upload is a POST, so no run goes past page 1 on the postings that
  require one (most of them). Their screening questions are checked in jsdom
  (two banks) and on 13 live postings of the three companies that need none.
- **Eightfold** (decision 100) and **ADP's Community Care Plan** (Apply opened
  nothing live: a cookie overlay, then a bot-check POST): not run.
- **Dayforce has no bank**: its questionnaire loads only inside the apply
  flow (the posting page's data has none), so its questions are read only
  on the live pages run.
- **Paylocity's screener** comes after the first page's submit (a POST): its
  fixes (the states listed, "k" ranges and the thousands comma, the
  academic status, a month-and-year graduation, a skill's years) are checked
  in jsdom only. Its first page is pinned live (r8g-paylocity-*).
- **Pinpoint's equality monitoring lists** stay on their placeholder live, as
  in every Pinpoint case, so its self-ID fixes ("No Response", "Gay or
  Lesbian", mixed races, the protected veteran's decline) are checked in
  jsdom only.
- **BambooHR's Fabric selects** (Province, Country, Highest Education) are
  never filled live, in any BambooHR case: from before this round. Decision
  114 is seen on JazzHR's and SmartRecruiters' lists, not BambooHR's.
- **Pinpoint's question revealed by an answer**: "Have you previously been
  employed by Franklin Electric?" shows once "Are you currently employed by
  Franklin Electric?" is No (its JSON names the parent). The fill's
  revealed-question pass answers it in most runs ("1 question(s) appeared
  during the fill"), and it was blank in two of six (once on each posting);
  each case passes alone. A probe (every non-GET blocked) shows the page
  mounts the question 43 ms after the No, visible at once, so the settle
  wait is not the cause; in a miss the revealed pass ran (its phase took
  1.5 s) and found nothing new. Not reproduced on demand. Once, in the
  final re-run, Gujarat's fill wrote nothing on the form at all ("Nothing on
  this page was filled", after a scan that found 16 fields to fill); alone
  it passes, 22 of 22.
- **A salary list that names no currency, on a posting whose place is not
  read** (NISC's "Any NISC Location" on Greenhouse): a figure is taken as it
  stands, so a Canadian's 95,000 (CAD) took "91-95k". With the place read
  (NISC's other posting, in Missouri) it is dollars and the Canadian and
  euro figures stay blank. From before this round for lists in plain
  numbers; "k" lists are read only since this round.
- **UK sponsorship and right-to-work** (White Swan on Pinpoint): blank for
  every persona, none of whom mentions the UK (the scope rule of the 10-03
  overnight run: an applicant's sponsorship answer never covers a country
  they never named).
- **Riot's Discipline** ("Psychologie") stays blank in runs: the embed
  re-renders mid-fill and the re-asked field missed once more. Its second
  education row (Concordia's certificate) is added in one run of four.
- **Humble Robotics' location** ("San Jose, CA") empties under batch load;
  alone it fills (the picker takes "San Jose, CA, USA" in tests). Lever's
  search lists nothing within the adapter's 12 s then (Humble in the last
  pass and again in the final regression, Azul once before it); the report
  now says so.
- **Pure Storage's School** went blank in the last pass under batch load (the
  re-ask read no options and went to the backend); the two passes before it
  and the run alone filled it.
- **SmartRecruiters clicked mid-render** (Bosch HR in the final regression,
  M3 in the last pass; Boston Engineering's blind run, whose labels were
  element ids, before them): the
  fill started 6.5 s after load (8.7 to 10.7 s when M3 passed), while the
  controls still carried Lit's template markers ("?lit$…$") for labels. City
  reached the fill with no label, went to the backend and was dropped (the AI
  is dead), and Website was not filled. Alone each passes. Not reproduced on
  demand. The likely gap: the fill's settle wait watches only the top
  document's child list, while SmartRecruiters renders inside shadow roots
  and may commit labels as attributes, which no observer here watches (on
  purpose: the extension's own attribute writes would set it off).
- **"If Other, please specify" boxes** (IMC, ServiceNow) get the major even
  when the list holds it: from before this round.
- **Waymo's careers site** answered every load after the final re-run with
  an AWS WAF challenge (its form frames come back 202, empty), whose POST a
  run blocks: the case cannot run until the site lets this machine through.
  Its new education block's School, Degree and Discipline (lists that load
  their options from Greenhouse's API as one types) stay blank live, as the
  start date does (the profile has none).
- **The H-1B ITAR question** (an H-1B holder is no US person: unknown to the
  profile) and **LinkedIn's "degree type" BA/BS** for a BASc: blank.
- **D-61** blanks (section 2e): worth a look by whoever owns the allow-list.
- **Jobvite past the consent gate, iCIMS past its login, Workday past its
  account wall**: not this round's (each needs a POST or an account).
- **OpenAI** (billing down since 08-12): essays and AI-only answers stay
  blank.
- **Prod**: no autofill report since 2026-10-03.
- **CI's checks** were not run: nothing was pushed. Before a push, run them as
  the brief says.

# Round 7: Jobvite's consent gate, SmartRecruiters from a question bank to its screening step, new Greenhouse and Lever boards (2026-10-20)

Branch `night/autofill-round7` (worktree `.worktrees/autofill-round7`), from
main at 8574740 (round 6), pushed to main on 2026-10-20 at the owner's request
once CI's checks passed locally (section 8). Nothing uploaded, no release zip
built. Nothing was submitted anywhere and no account was created: every live
run blocks non-GET requests (GraphQL queries aside), and Nutanix's consent
stops at its POST.

The biggest find: **SmartRecruiters' screening questions were never read on a
real page.** The one-click flow could not see the step's "Next" (a
`<spl-button>` web component) and took the third-party "Apply With Indeed"
beside the form for the form's own submit, so it said "Review and submit" on
page 1. Pressing Next needs no POST, and once there, every question's text is
slotted into a label inside a shadow root, so the scan read 29 fields with no
label and said "No application form found". Both fixed: Western Digital's
screening step now fills 19 answers live, every one right.

The worst moment was my own: a longer wait for async selects (41971b8, meant
for a Degree dropdown that missed under load) emptied Greenhouse's School and
Degree on eight of thirteen boards. The live re-run caught it; it is reverted
(70fb5c9), and the re-run after that holds every education field.

## 1. Real users first

Prod (read-only SELECT, branch main), 2026-10-20: **3 autofill reports since
2026-10-03, all that day, all the owner's**: Salesforce's Workday "Apply
Manually" and sign-in pages (one field each, none failed) and Brex on
Greenhouse (6 failed), a case since round 4 (`r4r-embed-brex`, passing in
both regressions). Nothing after 10-03: no field captures, pending questions
or feedback; no extension sign-in code; no application record since 10-17;
security events since 10-08 are 11 token refreshes. So P0 had no new real
fill to turn into a case.

Active postings in prod (not duplicates), by host: Workday 1,607,
Greenhouse 1,128 (163 boards, company sites with `gh_jid` included), Lever
209, SmartRecruiters 175 (26 companies, Bosch 118), Ashby 136, iCIMS 83,
Workable 20, Jobvite 6, other 1,819.

## 2. Open from round 6

### a. Jobvite's data-consent gate (Nutanix): built, verified live

Built from the page's own markup (a426610; fixtures captured GET-only): a
`form[name="consentForm"]` posting to `/apply`, a location-of-residence
select whose options Angular adds after load, the policy text from
`consentFormDetail` ("By clicking Submit, I acknowledge…"), then Submit. The
extension picks the applicant's country, else the list's any-location option
("All Locations (English)"), never a bare "Other"; the consent button is the
step's advance whatever it says (decision 70). Live, both Nutanix cases pick
"All Locations (English)" and park on "Step 1 filled. Review this page, then
Next page"; one press clicks Submit and the only request is the blocked `POST
…/nutanix/job/oi8PAfwz/apply`. A step that filled nothing never says "Done"
any more ("Nothing on this page was filled", decision 71). Only Nutanix of
prod's six Jobvite companies has the gate. Both cases are pinned.

### b. Carvana's custom selects: the read-back

The widget keeps its prompt above the pick, each in its own element, so the
button reads "Select answerYes"; the prompt filter took the whole string for
a prompt and every pick read back empty, hence "reverted,
value_cleared_after_write" while the page held the value. The read now takes
the button's text pieces that are no prompt (e3b8c84, tested on the widget's
real markup before and after a pick). The pins were not touched.
Live, in the full regression on the final build: 9/9, **none reverted**,
its selects recorded as filled.

### c. SmartRecruiters' label fallback: cannot fire; the gap was slots

Run on a CPU slowed sixfold with Autofill pressed the moment it was enabled
(ServiceNow), the scan saw no control, then fourteen already labelled
("First name*"): input and label arrive in one render, so the host-attribute
fallback (1161e4b) has nothing to catch. It stays as a unit-tested guard and
the probe case is retired with that comment. What really emptied
SmartRecruiters' labels was the screening step's slotted text (section 3),
fixed in 85393d0 and verified live.

### d. D-61 (main's work-authorization allow-list): left as decided

Not overridden. Its blanks this round: Western Digital's "Are you a citizen
of the country you'll be employed in?" and LLNL's dual-citizenship question
for the H-1B and OPT personas, whose statements give no status under it. The
four regression cases pinned blank for it in round 6 stay pinned blank.

## 3. New ground

### SmartRecruiters (175 active postings): a question bank, then the real pages

**Bank.** SmartRecruiters' one-click page carries its screening questions in
`window.__OC_CONTEXT__.screeningConfiguration`; a plain GET of the page is
refused (DataDome), so `tools/qbank-smartrecruiters.mjs` opens each posting
in a fresh headful browser with every non-GET request aborted and reads the
context. **41 postings of 24 companies, 678 questions, 4,068 answers** (six
personas). Read blind, every wrong answer and answerable blank fixed with a
failing test first (`bankSmartRecruiters.test.ts`), every bank re-run and
every changed answer read (section 7).

**Live, 13 postings** (r7b-sr-*). DataDome lets one page through per fresh
browser on this machine, so each case runs in a browser of its own (the
regression runner now does this for every SmartRecruiters case). Page 1
fills on all 13: names, the confirm-email box, the national number beside the
code picker, LinkedIn, website, the right city suggestion. Then the step:
the flow now finds "Next" (75c5471) and the screening step's labels and
radios read through their slots (85393d0). Three postings reach their
screening step in a run (ServiceNow's ML role, Western Digital, Intuitive);
the other ten hold page 1 for a required résumé a run cannot upload, as a
person would. On those three, every write was read: Western Digital 19
answers (work right Yes and sponsorship No for a green-card holder, US
citizen No and US person Yes, never employed by WD No, the GPA band, the
graduation month and year, the self-ID answers), ServiceNow 10, Intuitive 8.
All 13 were pinned (page 1); CCSF's posting closed during the night and its
case is retired.

### Greenhouse: thirteen more boards

Of prod's 163 boards, 29 were in no bank and no case (Amperesand's posting
has closed). Thirteen ran live blind (r7a-gh-*: Gallup, Duolingo University
Recruiting, National Life, Axon, GCM Grosvenor, Artefact, Inversion Space,
Arcesium, Perpay, Alten, AMCA, Vestmark, 66degrees), every one of 228 writes
read. Their wrong answers and blanks are in sections 4 and 5. The re-run on
the fixed build caught the education regression above; the re-run after the
revert holds every School, Degree and Discipline and shows each fix on the
real page. All thirteen are pinned.

### Lever: thirteen more boards

Of prod's 37 Lever boards, 15 were in no bank and no case; thirteen ran live
blind (r7c-lever-*: Artera, Azul, Behaviour, Brillio, CesiumAstro, CFS
Energy, FieldAI, Galatea, GenBio, Rhombus, SoloPulse, Viget, Waabi), every
write read, then re-run on the fixed build: every fix shows live. All
thirteen are pinned.

## 4. Wrong answers fixed

Live pages (the value written, and now):

| page | question | wrote | now |
| --- | --- | --- | --- |
| SmartRecruiters, every posting (13 live) | the step's button | "Apply With Indeed" taken for the form's submit: "Review and submit" on page 1 | "Next" (a `<spl-button>`): "Step 1 filled. Review this page, then Next page" |
| SmartRecruiters Western Digital (screening step) | 29 questions | read by no label: "No application form found on this page" | read through their slots; 19 answered |
| Jobvite Nutanix (2 live) | the data-consent gate | nothing selected, and "Done … Review and submit" on the gate | "All Locations (English)"; Submit offered as the step |
| Greenhouse, 8 boards (re-run on 41971b8) | School, Degree, Discipline | emptied after writing ("option , selected") | held (41971b8 reverted) |
| Greenhouse Arcesium | "Current Mailing Address" | "Seattle, WA" | the whole address |
| Greenhouse National Life | "If your current employment eligibility is based on F-1 student visa OPT or another nonimmigrant status…, will you be seeking Company support…?" | No, for a US citizen | "Not Applicable" (decision 92) |
| Greenhouse National Life | "Have you executed any agreement with your current or prior employer …? … If no, type N/A" | No | "N/A" (decision 74) |
| Lever Azul | "Race" (radios), an applicant who states none | blank: Lever wraps the question in a `<label>`, and the first radio read as every option | "Decline to self-identify" (day-session decision 4) |
| Lever Brillio | "Current location" | blank: two Karnataka entries for Bengaluru tied | "Bengaluru, Bengaluru Urban, Karnataka, IND" |
| Lever Waabi | "Current location" | blank once: Lever's first search took more than 8 s | "Toronto, ON, CAN" (12 s wait) |

Question banks (each also a unit test, in `bankSmartRecruiters.test.ts`):

| company | question | wrote | now |
| --- | --- | --- | --- |
| Western Digital (SR) | "Are you currently party to any employment, consulting or personal services agreement that (i) restricts your right…" | Yes for three personas (read as a work-right question) | No |
| M3USA (SR) | "Are you eligible to work at M3USA without required sponsorship now or in the future?" | Yes for the H-1B and OPT holders | No |
| Resultant (SR) | "Do you currently hold Optional Practical Training (OPT) work authorization?" | Yes for an H-1B holder and a citizen | No |
| LLNL (SR) | "Have you had a job or internship paid by LLNL?" | Yes for three personas | No |
| LLNL (SR) | "Are you a dual citizen (including U.S. citizenship) or a legal permanent resident of the United States?" | Yes for a US citizen alone | No (decision 80) |
| Wellmark (SR) | "VERIFICATION … I certify answers given herein are true and complete…" | No | Yes |
| Wellmark (SR) | "I acknowledge … that I do not use tobacco or tobacco-like products…" | Yes for everyone | blank, kept from the AI (decision 72) |
| GDMSI (SR) | "What is the available term length for your upcoming co-op work term?" | "4 months" / "8 months" (read as a start date) | blank |
| Bosch (SR) | "When is your expected graduation date? … (MM/YY)" | "April 2027", "2014" | "04/27"; a year-only date gives nothing (decision 73) |
| RodeoFX (SR) | "… (JJ/MM/AAAA) What is the earliest date you would be available to start? (DD/MM/YYYY)" | MM/DD/YYYY | DD/MM/YYYY |
| Versant (SR) | "This role requires a final-stage onsite interview at one of our office locations … Are you able and willing to attend…?" | No for two who will not relocate | Yes (decision 77) |
| Veolia (SR) | a race list under its consent paragraph | read as gender identity: blank | the stated race (decision 90) |
| Veolia (SR) | "Please select" [Male / Female / Non-binary / I do not wish to identify] | the decline, for everyone | the stated gender |
| City and County of San Francisco (SR) | "In which area of study was the major coursework of your highest level of education?" | "Master's Degree", "Diploma" | the field ("Information Systems") |
| SBT, City and County of San Francisco (SR) | "Please type in a language that you speak" | "English (Native), French (Professional)" | "English" (decision 83) |
| SoFi (Greenhouse) | "Do you currently hold, or intend to hold, any FINRA licenses if employed by SoFi?" | No (read as a former employer) | blank: an intent |
| HAI Group (Greenhouse) | "In summer 2027, what year will you be rising into?" | "Senior", for an April 2027 graduate (mid-round) | "I will have graduated college/university" (decision 75) |
| Robinhood (Greenhouse) | a three-part government-official question with "Have you been referred … by a government official?" | Yes for the applicant an employee referred (mid-round) | No (decision 85) |
| Riot (Greenhouse) | "In which language(s) are you business fluent?" | Hindi, stated conversational | English only (decision 82) |

Caught mid-round by the bank re-runs, and fixed before anything else ran:
"opt in" read as OPT (Lucid's SMS question, Rave Aerospace, Salesloft), a
gender identity list read as gender, the graduation month of a year-only date
sent to the AI, and the two above marked mid-round.

## 5. Blanks now answered

| page | question | now |
| --- | --- | --- |
| Lever Galatea (live) | "In which year would you be available to start?", "In which quarter…?" | the stated start's year and quarter (decision 84) |
| Lever CFS Energy (live) | "…Are you one of the following: a U.S. citizen, a U.S. national, a lawful permanent resident … or a protected individual as defined by 8 U.S.C. 1324b(a)(3)…?" | No for a Canadian (the sentence ended at "U.S.C.") |
| Lever SoloPulse (live) | "Are you able to hold a U.S. Security Clearance?" | No for a Canadian (decision 81) |
| Lever SoloPulse (live) | "Are you currently a University student in the Atlanta area pursuing an Engineering degree?" | No for a graduate (decision 88) |
| Lever Behaviour (live) | "Do you have professional working proficiency in:" [English / French] | both, for a fluent French speaker (decision 82) |
| Lever Behaviour, Viget (live) | "…a 4 months internship at 37.5h/week?", "…a standard 40-hour, Monday-Friday work week…?" | Yes (decision 86) |
| Lever Artera (live, by test) | "Would you require any form of Sponsorship or Visa? If Yes, please elaborate below." | No for someone who needs none (decision 87) |
| Greenhouse Gallup (live) | "Please indicate your level of education during the Fall 2026 Semester:" | "None of the above" for a graduate |
| Greenhouse Gallup (live) | "Do you currently attend a college or university within an 80-mile radius of San Francisco, CA?" | No for someone enrolled nowhere |
| Greenhouse Gallup (live) | "Please indicate which languages you speak in addition to English:" (a remix multi-select) | "Korean" (the AI's before, never written with it down) |
| Greenhouse Duolingo (live) | "What is your expected graduation timeline?" | "Anything before Fall/Winter 2026" for earlier graduates |
| Greenhouse Duolingo, GCM (live) | Degree | "Bachelor's Degree" (missed under load in the blind run) |
| Greenhouse Axon (live) | "…would you meet the requirements for a deemed export license…?" | Yes for a US person |
| Greenhouse Axon (live), Torc (bank) | contractual obligations / agreements that restrict joining | No |
| Greenhouse AMCA (live) | "ITAR Status" | "Not Applicable" for someone who is no US person |
| Greenhouse National Life (live) | "Are you involved in any business activity … If no or not applicable, type N/A." | "N/A" |
| ServiceNow (SR bank) | "A. … an employee of the U.S. Federal Government?", "B. …", "C. … a state, local, or municipal government entity…" | No for a history with no government employer |
| Bosch, GDMSI, Resultant, Plot, IMC (banks) | class standing, year of program, current year of studies, degree type | the final year for a final-year student |
| EVERSANA, M3USA, Lucid, Wattpad, National Journal, CCSF (banks) | highest level completed, for a student | the high school diploma (decision 76) |
| Cricut (SR) | "Are you CURRENTLY living in/around the Salt Lake City Metro area?" | No for everyone elsewhere |
| ServiceNow, CRB (SR) | "If applicable, would you consider relocating…?" | the stated willingness |
| Circle Logistics (SR) | "Will you now or in the future require a visa to work in the United States?" | the sponsorship answer |
| M3USA (SR) | "Did someone from our company suggest you apply? If so, please provide their name – if not type N/A." | "N/A" |
| Profluent (Greenhouse) | "Are you currently on OPT or CPT?" | Yes for the OPT holder, No for everyone else |
| Vialogic (Greenhouse) | "Do you currently hold dual citizenship with another country?" | No for one stated citizenship |
| Palantir (Lever) | "Are you eligible to obtain the security clearance specified…?" (Kitsap, WA) | No for non-citizens |
| EQT (Greenhouse) | "Previous EQT Employee", "Related Employee(s)" (asked in their descriptions) | No |
| Coveo, TeleTracking, Cricut, CRB (banks) | referral questions | No, or Yes where a referral is stated |
| Niantic (Ashby) | "In Office Requirement" (asked in its description) | Yes for those willing to relocate (decision 49) |
| Wellmark (SR) | two bot checks and "Is this application form being completed by an AI tool?" | kept from everyone, the AI included (decisions 78, 79) |

## 6. Decisions (one rule each, each easy to reverse)

70. Jobvite's data-consent gate is a step of its own: its residence list
    takes the applicant's country (the English variant where it has
    several), else its any-location option, never a bare "Other"; its consent
    button is the step's advance whatever it says; live runs stop at its POST.
71. A step that filled nothing, with an empty fillable field left, never says
    "Done": the flow stops with "Nothing on this page was filled".
72. A personal habit (tobacco, nicotine, vaping) is never pledged for the
    applicant, nor sent to the AI.
73. Dates go in the format the question states (DD/MM/YYYY, JJ/MM/AAAA,
    MM/YY, …); a part the profile lacks (the month of a year-only
    graduation) gives nothing, and the AI is not asked for it.
74. "If not, type N/A" takes "N/A" when the underlying answer is No.
75. Class standing: a bachelor's student graduating within the academic year
    asked is in the final year ("Senior", "4th Year or Above"); earlier years
    stay theirs (no profile holds a program's length); a term after the
    graduation finds them graduated; someone enrolled nowhere is the list's
    "None of the above", or its graduated option when they hold a college
    degree.
76. A student's highest level completed is the high school diploma their
    degree program implies (decision 67, for students).
77. Attending an onsite interview is travel, not a move: Yes, whatever the
    relocation answer.
78. A question that tells people from bots ("What is the third word in this
    sentence?", "…used to monitor web traffic authenticity") is never
    answered, nor sent to the AI.
79. "Is this application form being completed by an AI tool?" is the
    authorship pledge (decision 6): never answered, never sent to the AI.
80. One stated citizenship is no dual citizenship; someone not allowed to
    work in a country, or there on a visa, is neither its citizen nor its
    permanent resident.
81. A clearance a country grants only its citizens: being able to obtain,
    hold or achieve one is No for anyone who is not its citizen, whatever
    clearance the profile names; a citizen who holds none answers it.
82. A language list takes one option per stated language (the bare name,
    else the one variant for the applicant's country), never the language the
    question leaves out; a level asked (professional working proficiency,
    business fluent) takes only languages stated at that level.
83. A mailing or home address asked in one box is the whole address; a box
    for one language takes one language, without its level.
84. A start asked as a year or a quarter is the stated earliest start's (today
    once that has passed).
85. A question about government officials (holding office, being referred by
    one, being related to one) is the government question, never the
    referral or relatives default.
86. A schedule the posting sets (a 40-hour, Monday-Friday work week; N hours
    a week) is a requirement accepted by applying (decision 49), unless the
    applicant prefers part-time work.
87. A sponsorship need whose details are asked only on Yes is No for someone
    who needs none; a Yes is theirs to explain.
88. A university student in a named area is No for someone enrolled nowhere;
    a student's school stays theirs to place (decision 48).
89. Another site's apply beside a form ("Apply With Indeed", "Apply with
    LinkedIn") is never the form's own button.
90. A self-identification list is read by its options (race options under a
    consent paragraph are the race question), except that a gender identity
    label stays gender identity.
91. One city listed under two districts of the region the applicant names is
    one place: the lookup's first.
92. A question conditioned on a nonimmigrant status (F-1 OPT, TN, E-3, …) is
    "Not Applicable" to a citizen or permanent resident; another visa holder
    answers it.

## 7. Regression

**Question banks.** Every bank re-run on the final answer code against
round 6's main (the base), every changed answer read:

| bank | answers | changed since the base | in questions |
| --- | --- | --- | --- |
| Greenhouse 1 | 11,256 | 36 (15 of them dates: the runs straddled midnight) | 10 |
| Greenhouse 2 | 10,068 | 37 (4 dates) | 11 |
| Greenhouse 3 | 9,528 | 15 | 5 |
| Ashby | 4,908 | 5 | 2 |
| Lever | 6,732 | 16 (9 the order of the same languages) | 4 |
| Workable | 4,926 | 0 | 0 |
| Jobvite | 5,952 | 0 | 0 |
| SmartRecruiters (new) | 4,068 | 315 | 42 |

The re-runs between fixes caught six regressions of this round's own ("opt
in" read as OPT on Lucid, Rave Aerospace and Salesloft; a gender identity
list read as gender; a graduate's month sent to the AI; HAI's summer;
Robinhood's referral; LLNL's No gone blank), each fixed with a failing test
first before the next run.

**Live.** The full regression on the final build, headful: 271 pinned cases
(round 6's 230, Nutanix's two pinned this round, round 7's 39), 20 batches
of 13 and each SmartRecruiters case in a browser of its own. **259 passed the
first time.** Every failure was read:

| cause | cases | what now |
| --- | --- | --- |
| the posting closed (Beacon and Base Power on Ashby, PointClickCare's analyst on Lever, Home Depot on Workday, CCSF on SmartRecruiters, which closed during the night), each checked on the ATS's public API (GET) | 5 | retired, each with why |
| answers this round's rules give where the case had none: Shield AI's "…able to obtain, a S/SAR clearance?" No for a Canadian (decision 81) and its race decline (Lever's nested label); Wattpad's highest degree for a student (decision 76) | 2 | pinned |
| the calendar: National Journal's start bucket is "Within 2 weeks" now that the persona's start is 13 days off | 1 | pinned as the bucket that follows the date |
| pins missing for page values: Workday's new language picker (Revvity, drawn after the first snapshot), SmartRecruiters' country code (ServiceNow, RodeoFX) | 3 | pinned |
| Degree under batch load (GCM Grosvenor, "Couldn't open the dropdown") | 1 | passes alone, 24/24 |

Rescored against the new pins (`rescore.mjs`, no browser), with GCM's run
alone: **all 266 remaining cases pass.** Carvana, 9/9 with nothing reverted,
is P1b's live check.

## 8. Verification

- Unit: 2,979 tests pass (1 skipped: the opt-in question bank), typecheck,
  build, `scan-smoke`; every fix has its failing
  test first (`bankSmartRecruiters.test.ts`, `leverLive7.test.ts`,
  `srScreening.test.ts`, `jobviteConsent.test.ts`, `round7.test.ts`,
  `advance.test.ts`, `leverAdapter.test.ts`, `multiDrive.test.ts`,
  `flowController.test.ts`).
- Banks: section 7. Live, headful, GET and GraphQL queries only: Greenhouse
  13 three times, Lever 13 twice, SmartRecruiters 13 twice plus the probe,
  Nutanix 2, the checks after each live fix, and the full regression.
- Before the push to main, CI's checks ran locally on the branch (origin/main
  had not moved from 8574740): backend pytest from a scratch directory on
  SQLite, 4,084 passed; extension typecheck, build, `scan-smoke` and the
  2,979 unit tests; frontend `tsc` and `vite build`.

## 9. Needs manual verification, or still open

- **SmartRecruiters' screening step on a full application**: three of
  thirteen postings reach it in a run; the others hold page 1 for a résumé a
  run cannot upload. Its bank answers are verified in jsdom; on the real step,
  three postings.
- **Greenhouse's remix multi-selects**: they open for no scripted event; a
  failed engine pick now falls back to the page-world driver, item by item.
  Verified on Gallup's language list only.
- **Degree under load**: in a batch of live pages Greenhouse's Degree
  sometimes misses ("Couldn't open the 'Bachelor' dropdown"); the fix tried
  for it emptied the education section and is reverted. Re-run alone, it
  fills.
- **D-61** blanks (section 2d): worth a look by whoever owns the allow-list.
- **Jobvite past the consent gate**: Submit POSTs; what follows is unexplored
  live.
- **iCIMS past the candidate login, Workday past the account wall,
  SuccessFactors**: unchanged from round 6.
- **"Which location are you applying for?"** (Azul: Sunnyvale or United
  States - Remote) took the remote option for a Boston-area student, from the
  location's country: plausible, but the applicant's call.
- **A family member's government employment** ("D. … a member of your
  Immediate Family …", ServiceNow) is answered No by the relatives default,
  as before this round. Worth a look: many families include a public
  employee.
- **The remaining 16 Greenhouse boards** in no bank and no case, and Lever's
  Deep Genomics and Significance: not run.
- **OpenAI** (billing down since 08-12): essays and AI-only answers stay
  blank.
- **Prod**: no autofill report since 2026-10-03.

# Round 6: iCIMS behind its frame, Workday to the account wall, Jobvite and new Greenhouse boards (2026-10-17)

Branch `night/autofill-round6` (worktree `.worktrees/autofill-round6`), from
main at f7796e3 (round 5 plus the profile-facts work). Local only: NOT
pushed, NOT deployed, nothing uploaded, no release zip built. Nothing was
submitted anywhere and no account was created: every live run blocks non-GET
requests (GraphQL queries aside), and every page here stops at a POST.

The biggest find: **iCIMS never worked past its job page.** Round 5 blamed the
frame the job sits in; the entry search already reaches same-origin frames.
The Apply button's hidden short label made its text "Apply for this job
onlineApply", and once clicked, the candidate login opened inside the frame
while the top frame's flow, finding no form of its own, stopped and cleared
the state the frame resumes from. Thirteen iCIMS postings now reach the
candidate login and fill it.

## 1. Real users first

Prod (read-only SELECT, branch main), 2026-10-17: **no autofill report since
2026-10-03** (183 in all), no field captures, no pending questions, no
feedback since 08-14, no extension sign-in code since 09-28; the only
security events since 10-08 are ten token refreshes. One application was
recorded since round 5: the owner's, a LinkedIn posting marked applied in the
web app, with no questions. Vercel's runtime logs are still 403 to the MCP
(the CLI is not installed). So P0 had no real fill to turn into a case.

What prod lists instead (active postings, not duplicates): Workday 1,613,
Greenhouse 572, Lever 216, SmartRecruiters 166, Ashby 144, iCIMS 89, Workable
20, Jobvite 6. Round 6 went where that volume is and the tests were not:
iCIMS, Workday, Jobvite, and the Greenhouse boards in no bank and no case
(37 of prod's 117).

## 2. Open from round 5

### a. iCIMS: from the job page to the candidate login

| cause | now | verified |
| --- | --- | --- |
| The Apply button holds a long label and a hidden short one ("Apply for this job onlineApply"), so the entry search never matched it | the entry search reads what a control shows (`shownText`), its title only when it shows no text | live, 13 postings |
| The click opens iCIMS's login inside the posting's frame; the top frame's flow found no form, said "No application form found", and cleared the state the frame resumes from | the flow hands over to the frame that owns the form, and waits up to 6 s after an entry click for one to claim it | live |
| A panel that had loaded the profile never gave it to a frame it adopted later | it does | live, and `overlayAdoptFrame.test.ts` |
| The login's Next is disabled until the email and the privacy notice are in; a one-field step drew no form scope; the button is an `<input value=…>` | the flow waits up to 3 s for a disabled advance, scopes a lone field by its own form, reads an input button's value | live |
| Kinaxis' login is a full page whose email box has `autofocus`: the reconciler took the page's focus for the user's and never wrote the email | a box is the user's only after the user's own click, tap or key (`userFocus.ts`) | live |
| Schwab's login asks for a phone beside a "Country Code" link over 245 "(+93) Afghanistan" options: read to its 60th option, the only +1 left was Canada's; the top frame also scanned the frame's picker as its own | every option is read for resolution, the exact country name wins, the national number goes beside the picker, and a child page's controls are its own | live |
| H&R Block's posting had closed: iCIMS showed its job search, and the Location filter took the applicant's state | a site's own job search is page chrome (`siteSearch.ts`) | live: nothing written |

Behind the Apply on every posting: an email-first candidate login whose Next
POSTs behind an invisible hCaptcha. Live runs block it, so no case goes
further and no account is created. Thirteen postings are pinned (round 5's
eight r5c-icims cases, six re-pointed at open postings, and five r6a-icims):
the email, the privacy notice, Schwab's phone.

### b. SmartRecruiters' label fallback (ServiceNow)

Run alone, the session's first SmartRecruiters page (no DataDome page this
time): **12/12, City "Toronto, Ontario, Canada".** But the labels were again
read the ordinary way ("City*"), so the web-component fallback (1161e4b)
still never fired live. It stays unit-tested only (section 9).

### c. Lever's location wait, Databricks' Degree

Both passed in the regression: Databricks 25/25 (its Degree dropdown), SEP
17/17 and Wattpad 29/29 (Lever's location typeahead with its 8 s wait).

### d. OneImaging's embed host page

The posting is open again (Greenhouse's API answers 200 with its 14
questions). Both cases are restored and pass live: `live-gh-oneimaging` and
`live-gh-oneimaging-embedded`, 16/16 each, the second through the frame the
panel adopts.

### e. SuccessFactors

Unchanged: its Apply POSTs to `/services/cas/createpayload/`, which live runs
block. A manual check.

## 3. New ground

### Workday (1,613 active postings): to the account wall

No GET-only question bank exists: Workday's questions load only inside a
signed-in apply flow (its one pre-sign-in GET, `applyflowpages`, names the
pages, not their questions). So thirteen postings ran live, as far as a run
may go: the posting, Apply, the chooser, the create-account wall. **Twelve
reach the wall and fill it:** the persona's email, a generated password
twice, and the account consent where there is one ("I accept", AEP). Create
Account POSTs and is blocked: no account was created. Adobe's "Apply
Manually" starts with a POST, so its sign-in never draws (pinned with a note).
The twelve are pinned (r6b-wd-*); passwords by shape, never by value.

### Jobvite (six companies in prod): a question bank from the apply page

Jobvite writes each form into its apply page (`preloadedData`: `applyFields`
and `preScreeningFormFields`), so `tools/qbank-jobvite.mjs` builds a bank
with GETs only: each company's job list for titles and places, each apply
page for its form. **43 postings, 992 questions**, six personas, 5,952
answers. Read blind first (`jobvite-review-1`), then every wrong answer and
answerable blank fixed with a failing test first (`bankJobvite.test.ts`),
then every bank re-run and every changed answer read (section 7). The builder
renders Ross Video's "I Accept" (an `IAgreeCheckbox` under its data-consent
paragraph) as the box it is.

Thirteen of the bank's postings then ran live (r6d-jv-*). Every Jobvite form
shows its first step and stops at a captcha (the flow pauses there, as it
should), so the screening questions behind it stay the bank's to test. The
first step verifies three bank fixes on the real pages: "Address 2" takes
the unit ("Apt 12C", "Unit 805") on Ross Video, GEI and ActioNet; SRC's
"Applicant Source" is "Internet Career Site" for a LinkedIn applicant and
"Employee Referral" for a referral; ActioNet's highest education is "High
School" for a bootcamp graduate. Eleven are pinned. Nutanix opens on a
data-consent gate whose Accept POSTs: its two cases stay unpinned (section 9).

### Greenhouse: thirteen boards in no bank and no case

Of prod's 117 Greenhouse boards, 37 were in no bank and no case. Thirteen of
them ran live blind (r6c-gh-*: Astera, Kensington, Tenstorrent, Figure,
HeyGen, Point72, Everpure, Sigma, Justworks, Hasbro, CLEAR, Arc Institute,
PDT), every write read. They found the sponsorship misread that said Yes for
every citizen on Justworks, the visa-status box, the project essays, and four
blanks a stated fact answers (section 5).

Re-run live on the build with every answer fix (r6c-gh-2), the fixes hold
on the real pages: Justworks' sponsorship No for a green-card holder, its
documentation Yes, its spouse question declined; Sigma's visa status "US
Citizen"; Everpure's deemed export No, "Not returning to a program" and the
policy box checked; Kensington's former-employer No; PDT's essays left to the
applicant. One new miss: Everpure's School took "Austin Peay State
University" for "The University of Texas at Austin" (the blind run had found
the right school; the search answered differently). The write path matched
any shared word; it now matches a school by its own words (f2bdd5a, then
9936317 for Ashby's results). All thirteen are pinned, Everpure's School to
the school itself.

## 4. Wrong answers fixed

Live pages (the value written, and now):

| page | question | wrote | now |
| --- | --- | --- | --- |
| iCIMS, every posting (13 live) | "Apply for this job online" | not found: "No application form found on this page" | clicked; the candidate login filled in its frame |
| iCIMS Kinaxis | Email (the page's autofocus) | nothing: the focus read as the user's | the email |
| iCIMS Schwab | Country Code, a Boston phone | "(+1) Canada" (the list read to its 60th option) | "(+1) United States" |
| iCIMS Schwab | Phone | "+1 617 555 0148" beside the code picker | the national number |
| iCIMS DocuSign (closed: the employer's job search) | "Search Keyword, Category or Job Title" | the applicant's title (a search ran); the filters and "Sort by" sent to the AI | untouched |
| iCIMS H&R Block (closed: iCIMS's job search) | Location filter | the applicant's state | untouched |
| Greenhouse Justworks | "Will you now or in the future require sponsorship … (e.g. H-1B status, etc.) …?" | Yes for everyone, a citizen and a green-card holder included | No for citizens and permanent residents, Yes for a visa holder |
| Greenhouse Sigma | "What is your current visa status?" (a text box) | No | the applicant's own statement ("US Citizen") |
| Greenhouse PDT | two "describe a project … that demonstrate(s) your data analysis experience/skills" boxes | "SQL, Python, Tableau" | blank: the applicant's essay |
| Greenhouse Justworks | "Are you a spouse or domestic partner to a veteran?" | No, from the applicant's own service | "I don't wish to answer", the list's decline (decision 66) |
| Greenhouse Everpure | School (a remote search), "The University of Texas at Austin" | "Austin Peay State University" (one run of two) | "University of Texas - Austin": a school is chosen by its own words |
| Ashby Ramp, Grow Therapy, The Exploration Company (regression) | School, "University of Waterloo" | blank, from the fix above (f2bdd5a): Ashby's result is three boxes (name, country, domain) | "University of Waterloo", read by its first box |
| Breezy Vagaro (closed: its /apply opens the company's 39 openings) | "Apply" | clicked the first job's (a Director of DevOps role in Ahmedabad) and filled that application | no entry: Apply buttons leading to several postings are a list (decision 69) |

Question banks (each also a unit test, in `bankJobvite.test.ts` and
`round6.test.ts`):

| company | question | wrote | now |
| --- | --- | --- | --- |
| Ross Video, ActioNet (Jobvite) | "Address 2", "Address2" | the city line ("Seattle, WA") | the street's unit ("Unit 805"), or nothing |
| Ross Video (Jobvite) | "…Export Administration Regulations… Can you confirm that you meet these requirements and are eligible to comply?" | No for a US citizen and an Indian citizen | Yes when every stated citizenship and home is known and none is embargoed (decision 8) |
| Nutanix (Jobvite) | "Current Company / School" | a 2014 school for a working engineer | the current employer; a student's school; else nothing |
| Nutanix, ActioNet (Jobvite) | how did you hear (LinkedIn; a job board) | "Girls in Tech Job Board"; Indeed | "Other - please list source below"; a job board: blank |
| SRC (Jobvite) | "Applicant Source" (LinkedIn, a job board) | blank, then "Other" mid-round | "Internet Career Site" |
| SRC (Jobvite) | a VEVRAA paragraph over two veteran options | read as salary: blank | "I AM NOT A PROTECTED VETERAN" |
| 35 lists in the Greenhouse, Ashby and Lever banks | how did you hear (a job board) | a board nobody named (Indeed, Glassdoor, Handshake, "Diversity Job Board", …) | blank (decision 57) |
| Moment Energy, Recidiviz (Greenhouse) | "Briefly articulate why you believe your skills are a fit…", "(In 2-3 sentences) What about Recidiviz's mission…" | the skills list | blank: an essay |
| GenScript, Pacific Fusion (Greenhouse) | "I acknowledge that GenScript … will collect and process my personal information…", "I confirm that the information I've shared in this application and resume is accurate…" | blank: read as the résumé upload by "resume" | Yes |

Caught mid-round by the bank re-runs, and fixed before anything shipped:
"Some High School" for a bootcamp graduate with a diploma (Lucid, Clearway),
"Less than high school" for a student with nothing completed yet (Wattpad),
the applicant's job title for "Did anyone refer you to this position? if so,
who?" (Syska Hennessy), and a check in Twilio's "…will reflect my own work"
pledge box (decision 6 keeps it blank).

## 5. Blanks now answered

| page | question | now |
| --- | --- | --- |
| Greenhouse Everpure (live) | "Does the deemed export rule affect your employment by Everpure?" | No for a US citizen or permanent resident; anyone else answers it (decision 62) |
| Greenhouse Everpure (live) | "Will you be returning to an undergraduate or graduate program after the internship if selected?" | "Not returning to a program" for a graduate (decision 63) |
| Greenhouse Everpure (live) | "Personal Information Policy": one box, "Acknowledge/Confirm" | checked (decision 60) |
| Greenhouse Justworks (live) | "If requested, would you be able to provide documentation reflecting the education and employment history listed on your resume?" | Yes (decision 61); it was read as the résumé upload |
| Greenhouse Kensington (live) | "Have you previously been employed by any of our Range Group companies" | No, the profile showing none |
| Greenhouse Fivetran (bank) | "Are you currently or have you ever provided services to Fivetran (or any of its global subsidiaries or affiliates)…?" | No, likewise |
| Greenhouse Dr. Weng (bank) | "Please provide visa status and expiration if applicable." | the applicant's own statement (decision 58) |
| Jobvite Ross Video (bank) | "Have you previously been employed by, or contracted to, Ross Video or any of its entities?" | No |
| Jobvite Ross Video (bank) | "When would you be available to start a new opportunity?" (Immediately, With Reasonable Notice, Other) | Immediately without a job; With Reasonable Notice with one |
| Jobvite Ross Video (bank) | "I Accept" under its data-consent paragraph | checked |
| Jobvite GEI (bank) | "Have you ever filled out an application with us before?" | No |
| Jobvite GEI (bank) | "If selected for this position will you continue to work for another employer?" | No (decision 64) |
| Jobvite SRC (bank) | "What is your overall G.P.A.?" (Below 2.5, 2.6, …) | the stated GPA, on its own scale only |
| Jobvite ACPM (bank) | "…Are you available for Full Time work?", "Are you available to work in a full-time position?" | Yes (decision 49), unless the applicant prefers part-time |
| Jobvite Nutanix and ActioNet, Greenhouse Lucid and Clearway, Lever National Journal, Workable DISA (banks) | highest education, a bootcamp graduate with a high school diploma | the high school diploma (decision 67) |
| GEI, Nutanix (Jobvite), internshiplist2000 (Greenhouse) | how did you hear, LinkedIn stated and not listed | Other |

## 6. Decisions (one rule each, each easy to reverse)

52. A site's own job search (its keyword box, its filters, sort and page
    size) is page chrome, never an application field.
53. A same-origin child page that runs its own content script owns its
    controls; the top frame never fills them as its own.
54. Focus the page gave a box (autofocus) is not the user's: only a click, a
    tap or a key in the page makes a box the user's, and the fill stays out
    of it then.
55. iCIMS's candidate login is filled (the email, and its privacy notice
    under the consent decisions); its submit is the step's Next whatever it
    says. Live runs stop there (a POST behind hCaptcha), as at Workday's
    account wall.
56. Of several dialing codes naming the applicant's country, the one that is
    exactly its name wins ("(+1) United States", not "…Minor Outlying
    Islands").
57. A generic source stated ("Job board") never picks a named board; a named
    platform the list lacks is "Other"; a list's general job-site channel
    ("Internet Career Site") takes any job site; a company website never
    becomes "Other" beside a general career site.
58. A visa, immigration, citizenship or residency status asked in words is
    the applicant's own statement of it (the profile's work authorization),
    never a yes or a no.
59. Address line 2 is the street's second line: the unit, or nothing.
60. A box that only says "Acknowledge" (or "Confirm", "I agree") answers its
    own fieldset's legend: a notice or policy there is acknowledged;
    pledges (decision 6) and waivers (decision 51) keep their own rules.
61. Documents for the education and employment on the résumé, if requested:
    Yes. Proof of a right to work is the work-authorization reader's.
62. The deemed export rule: No for a US citizen or permanent resident;
    anyone else answers it.
63. Returning to a program after the internship: No for a graduate enrolled
    nowhere; a student answers it.
64. Keeping another employer if selected: No.
65. "Current Company / School": the job still running, else the school
    enrolled at, else nothing.
66. A spouse's or family member's military service is never answered from
    the applicant's own.
67. "Some High School" is high school unfinished; a certificate or college
    diploma implies the high school diploma when the list offers no closer
    level.
68. A status list offering both "authorized to work … for any employer" and
    "I require sponsorship" takes the sponsorship option for a status that
    ends (F-1 OPT, STEM OPT): the employer will have to sponsor, and that is
    what the question is for (Vital Lyfe on Ashby, regression).
69. Apply buttons that lead to two postings or more make a list of jobs, never
    a posting: the flow opens none of them (Vagaro, regression).

## 7. Regression

**Question banks.** Every bank re-run on the final answer code, against round
5's base (Jobvite against its own blind run), and every changed answer read:

| bank | answers | changed since the base | in questions |
| --- | --- | --- | --- |
| Greenhouse 1 | 11,256 | 32 | 17 |
| Greenhouse 2 | 10,068 | 38 | 13 |
| Greenhouse 3 | 9,528 | 15 | 8 |
| Ashby | 4,908 | 4 | 4 |
| Lever | 6,732 | 12 | 6 |
| Workable | 4,926 | 3 | 1 |
| Jobvite (against the blind run) | 5,952 | 663 | 20 |

The re-runs between fixes caught four regressions of this round's own, each
fixed with a failing test first before the next run (section 4, last
paragraph).

**Live.** The first full run (240 pinned cases) was stopped after three
batches: f2bdd5a's school matching had left every Ashby School blank (Ramp,
Grow Therapy, The Exploration Company), Ashby's result being three boxes.
Fixed in 9936317; the three passed live (15/15, 17/17, 18/18), and the whole
regression ran again on that build, headful, 19 batches of 13: **216 of 240
passed.** Every failure was read:

| cause | cases | what now |
| --- | --- | --- |
| the posting closed (8 postings: Superhuman x2, ACPM's oXWTAfw6 x2, Vagaro, Syntiant, two of DISA's, Pryzm, Teleskope) | 10 | retired, each with why |
| the page changed, the answers the same (4AG's new ids x2, CSC's label, Jeffrey M's removed export question and its Seattle-only on-site question, Vital Lyfe's new work-right list) | 5 | re-pinned |
| answers round 6's decisions give where the case had none (Zoox x2 "continue working for other employers" No, Carvana's commute Yes, ActioNet's unit on Address 2, National Journal never Indeed for a stated job board) | 5 | pinned |
| blank since main's work-authorization allow-list (D-61, owner decisions batch B; identical on f7796e3, before round 6): Credence's and OpenData's "U.S. citizen?" and Crest's "permanently authorized" for the H-1B persona, Genetec's "authorized to work in Austria?" for "German citizen (EU)" (Credence also asks a referral question now in place of how-you-heard: No) | 4 | pinned blank, with the reason |

(A third Superhuman case was retired after the stopped run: 11 retired in
all, 230 pinned cases left.) Rescored against the new pins, **all 230 remaining cases
pass.** Vagaro's closed posting also showed a real bug: its /apply now opens
the company's openings, and the flow applied to the first of 39 jobs. Fixed
(5e5915e), and every pinned case that enters through an Apply button (41:
iCIMS, Workday, BambooHR, Rippling, Oracle, Recruitee, Paylocity, Dayforce,
Breezy, Pinpoint, Epic) ran again on the final build: **all 41 pass**.

Round 5's watch items passed: Databricks 25/25 (its Degree dropdown), SEP
17/17 and Wattpad 29/29 (Lever's location typeahead).

## 8. Verification

- Unit: 2,898 tests pass (1 skipped: the opt-in question bank), typecheck,
  build, `scan-smoke`; every fix has its failing test first (`round6.test.ts`,
  `bankJobvite.test.ts`, `applyEntry.test.ts`, `comboboxAsyncTypeahead.test.ts`,
  `mainWorldDriver.test.ts`, `icimsLogin.test.ts`, `siteSearch.test.ts`,
  `childFrameScan.test.ts`, `overlayAdoptFrame.test.ts`, …).
- Banks: section 7.
- Live, headful, GET and GraphQL queries only: iCIMS 13 (pinned), Workday 13
  (12 pinned), Greenhouse r6c 13 twice (pinned), Jobvite 13 (11 pinned),
  OneImaging 2, ServiceNow 1, the Ashby School three, the full regression
  (240), the 41 Apply-entry cases.
- Not run: CI's backend pytest and the frontend build (nothing outside
  `chrome-extension/` and NOTES.md changed; run them before a push).

## 9. Needs manual verification, or still open

- **SmartRecruiters' label fallback** (1161e4b): ServiceNow ran alone as the
  session's first SmartRecruiters page and City filled (12/12), but by its
  ordinary label ("City*"): the web-component fallback still never fired
  live. Unit-tested only.
- **iCIMS past the candidate login**: Next POSTs behind an invisible
  hCaptcha, which live runs block; what follows (a password, the profile,
  the questions) is unexplored. Thirteen postings reach the login and fill
  it correctly.
- **Workday past the account wall**: Create Account POSTs; twelve postings
  fill the wall. Adobe's "Apply Manually" starts with a POST, so its sign-in
  never draws live.
- **Jobvite's second step**: every Jobvite form stops at a captcha after its
  first step, so the screening-question fixes (export confirmation, start
  date, prior application, another employer, GPA, VEVRAA, full-time, I Accept)
  are verified in the bank (jsdom) only; the first step's (Address 2,
  Applicant Source, highest education) are verified live.
- **Nutanix's Jobvite data-consent gate**: a location of residence and
  language, its policy, then Accept (a POST). The extension selects nothing
  there and the flow says "Done … Review and submit" on the gate. Not built:
  nothing past it can be checked live.
- **SuccessFactors**: unchanged; its Apply POSTs. A manual check.
- **The work-authorization allow-list on main** (D-61, owner decisions batch
  B; not this round's): no positive status from an answer holding a word
  outside its vocabulary, by design. So "German citizen (EU)" is not read as
  German citizenship (Genetec's "authorized to work in Austria?", the export
  confirmation stay blank), and "H-1B visa (transfer required)" gives no
  status (Credence's and OpenData's "U.S. citizen?", Crest's "permanently
  authorized" stay blank). Four regression cases are pinned blank for it.
  Worth a look by whoever owns D-61: these blanks cost real answers.
- **OpenAI** (billing down since 08-12): essays and AI-only answers stay
  blank. PDT's essay pins only forbid the skills list, so they keep passing
  when the AI writes again.
- **HeyGen's "portfolio of example videos"** takes the applicant's general
  website: worth a look by the applicant.
- **Carvana's custom selects** report "reverted" in telemetry while the page
  holds the value (each is pinned and passes): the panel may call them
  failed. Seen in the regression; not new this round.
- **Prod**: no autofill report since 2026-10-03; Vercel's runtime logs are
  403 to the MCP (the CLI is not installed).

# Round 5: the open bugs, Ashby's Yes/No, two new question banks (2026-10-08, night)

Branch `night/autofill-round5` (worktree `.worktrees/autofill-round5`),
from main at 0992cd4 (round 4 and the phase 1 wording). Local only: NOT
pushed, NOT deployed, nothing uploaded. Nothing was submitted anywhere:
every live run blocks non-GET requests (GraphQL queries aside), and no
account was created on a real site.

The biggest find: **no Yes/No question on any Ashby form had ever been
answered.** Ashby draws them as two buttons over a hidden checkbox, and the
scanner saw only the checkbox, labelled "No". Work right, sponsorship,
relocation, office days, U.S. person: blank on every Ashby application
until tonight.

## 1. Real users first

Prod `autofill_reports` (read-only SELECT, branch main): **no reports since
2026-10-05.** The newest is still 2026-10-03 15:15 UTC (checked at the
start of the night and again at 06:25). Prod `security_events` shows no
extension sign-in since 2026-09-28, and none rejected. So P0 had no new
real fills to turn into cases; the night went to round 4's open bugs and
new ground.

## 2. The open bugs from round 4

| bug | cause | now | verified |
| --- | --- | --- | --- |
| a. Ashby's second school "Field no longer found" (Superhuman) | While a school typeahead is open, Floating UI marks every other field block `aria-hidden` (with `data-aria-hidden`); a rescan then saw 12 of 25 fields | The library's marks are not hiding. A dropdown whose control has gone is looked up again after the page settles | live: menu open, 25 fields stay 25 (old build: 12) |
| b. Workable Saalex freeze: does a person changing the State freeze it too? | | No. Three runs (a person alone; the extension, then a person, twice), each changing the State twice after the dates were in: no freeze | live probe |
| c. Workable's Address from the visitor's IP | Workable's form JSON serves its guess with `prefilledByLocation: true`, and the fill never overwrites a value | A box still holding exactly the guess is empty to the fill and gets the profile's location; a box the person changed is theirs | live, both ways; 20 Workable pins now read the persona's location |
| d. Paylocity's State, and "How did you hear" reported "No option matches" | State: a click only focuses the search box; ArrowDown or typing opens a virtualized list of role-less rows of state codes. The radio: choosing an option replaces its element | Role-less rows are read and a state is searched by its code; a named radio group is read again when one of its radios has left | live: State TX, "2 steps filled (19 ok)" (was 17 ok, 2 need attention) |
| e. Dayforce's Preferred Contact Method | went to the AI | the email the application gives, when the list offers it | live, pinned. Start dates stay blank (decision 28, month-only dates) |
| f. BambooHR Armstrong; Bosch and ServiceNow on SmartRecruiters | Armstrong loaded and passed all four cases (59/59): round 4's misses were the page. SmartRecruiters challenges every page after the first from this machine | Armstrong stays; Bosch retired so the suite's one SmartRecruiters page can pass first | live |

Found on the way: **today's date in the box's own format.** Saalex's date
boxes show DD/MM/YYYY (an en-GB browser): 8 October went in as 10 August,
and round 4's pins had let 10 May stand for 5 October. Today is now
re-emitted in each box's format, and pins check it against the run's date
(`{ today: "DD/MM/YYYY" }`).

## 3. New ground: two question banks, then their pages live

Round 4's method: blind run first, every answer read, a failing test
before each fix, every bank re-run after each fix and every changed answer
read, then the bank's own pages live.

| bank | source (GET only) | postings | questions | answers read |
| --- | --- | --- | --- | --- |
| Ashby | the public posting API lists a board's jobs; each form from the page's own `ApiJobPosting` GraphQL query (never a mutation) | 69 (the Ashby boards in prod `scraped_jobs`) | 818 | 4,908 |
| Lever | the public postings API; each posting's own `/apply` page as served | 55 (the 52 Lever boards in prod) | 1,122 | 6,732 |

With round 4's four banks: six banks, 47,418 answers, all re-run after
every fix tonight. New tools: `tools/qbank-ashby.mjs`,
`tools/qbank-lever.mjs`, `tools/qbank-diff.cjs` (every answer that changed
between two runs).

Then each bank's own pages live, each posting with the persona one of its
questions got wrong: 13 Ashby postings (`r5a-*`) and 13 Lever postings
(`r5b-*`), every write read, then pinned (150 and 230 checks).
The live runs found what the banks could not:

- **Ashby's Yes/No buttons** (above), and **a form's later sections**
  dropped when every field the scanner recognizes sits in the first one
  (TensorWave: both Yes/No questions cut off as "outside the form").
- **Long Lever questions lost their label**: a question with its
  description, or a paragraph long, ran past 300 characters and was named
  by its input (`surveysResponses[…][field3]`), so no rule saw it.
- **Lever's company** is only in its header logo's alt; unknown, the
  company's own careers-site option ("Palantir Website") was not its own.

Not built: a **Jobvite** bank (its apply page loads the form by XHR from
an API that needs a browser capture first; prod has 3 live Jobvite
postings) and a **SmartRecruiters** bank (the posting page and its API sit
behind the bot challenge from this machine).

**iCIMS and SuccessFactors, the families seen least** (P2), on 11 open
postings (`r5c-*` in `real-live-round5.mjs`, no pins, up to two next
pages): **none reached an application form, and nothing was written.**
Each started on the posting's own job page, as a user arriving from Tailrd
would, and on none did the panel open by itself (no form, no ATS match on
a job page); the run opened it.

- **iCIMS (8 postings):** every one shows the job inside a same-origin
  iframe (`…/job?in_iframe=1`), and its "Apply for this job online" lives
  there. The flow looks for an Apply button only in the frame it runs in,
  so it ended "No application form found on this page". (Its wording was
  missed too; fixed, but that fix could not show live, since the button was
  never in the flow's frame.) Behind it, an earlier round's page capture
  found iCIMS's candidate login by email, before any form.
- **SuccessFactors (L3Harris x2, Acuity):** L3Harris' "Apply now »" was
  missed (the arrow broke the anchored match; fixed). Now both L3Harris
  pages and Acuity click their Apply, but SuccessFactors starts the
  application with a POST (`/services/cas/createpayload/`), which every
  live run blocks, so the page never changes: "Couldn't open the
  application from this page".

The harness itself hid all this at first: it waited out three minutes per
page because it only recognized a stop that began "Autofill flow stopped",
so "No application form found…" looked like a hang. Fixed (65b564b).

## 4. Wrong answers fixed

Live pages (the value written, and now):

| page | question | wrote | now |
| --- | --- | --- | --- |
| Ashby, every posting (live: Iambic, Pryzm, Zip, Teleskope, TensorWave, Barnes, Replit, Base Power, Saronic, Snowflake, Amplitude) | every Yes/No question | nothing | answered by the rules |
| Ashby TensorWave | the form's second section | dropped as outside the form | scanned |
| Ashby TensorWave | "Where are you currently located?" (Ashby's location box) | blank | the location |
| Workable Saalex | signature dates | 10 August for 8 October | today, DD/MM/YYYY |
| Workable (20 postings) | Address | the page's guess from this machine's location | the persona's location |
| Paylocity | State | "Select a state" | TX |
| Lever eqbank | "Do you self-identify as a racialized person?" (an Asian persona) | labelled by its input name, blank | Yes |
| Lever Kobie | "Are you currently located in one of these states? Colorado, …" (Denver) | labelled by its input name, blank | Yes |
| Lever Palantir | how did you hear (stated "Company website") | Other | Palantir Website |
| Lever Wattpad | "Please select an identity that best represents you:" (White, cisgender, straight) | never selected | I don't identify as a minority |
| Ashby Superhuman (regression) | the two education rows (a Montreal career changer) | Concordia University, its certificate and major, in both rows | each row its own: Université de Montréal, then Concordia |
| Greenhouse Waymo (regression) | "If yes, what kind?" (a Canadian student) | TN (Applicable for citizens of Canada or Mexico) | blank, as in round 4 |
| Ashby Ramp (regression, 3 cases) | "pursuing a Bachelor's, Master's, or PhD in … Engineering, or another quantitative field?" (engineering students) | No (read as the PhD alone) | Yes |
| Ashby Grow Therapy (regression) | expected graduation, an April 2027 graduate (no spring 2027 offered) | Winter 2027 | blank |
| Ashby The Exploration Company (regression) | "…provide your own housing, relocation, and transportation to the internship site?" | Yes (from the willingness to move) | blank, the applicant's |
| Lever SEP, Wattpad (regression) | "Current location" while Lever's search was slow | cleared after 4 s (once each) | waits up to 8 s for the list |
| SuccessFactors L3Harris (batch C) | the job page's "Apply now »" | not found: "No application form found on this page" | clicked (the next step is a POST, see section 3) |

Question banks (each also a unit test, in `round5.test.ts` and
`bankLever.test.ts`):

| company | question | wrote | now |
| --- | --- | --- | --- |
| Iambic, Pryzm, Vital Lyfe (Ashby) | live there, or willing to relocate (Toronto, Berlin, Bengaluru, willing) | "Yes, I live in San Diego"; "In Boston"; "Yes, but require relocation" never chosen | the move option |
| TensorWave (Ashby) | "Will you now or in the future require authorization to work in the United States?" | backwards for everyone | Yes for anyone needing sponsorship, No for a citizen |
| Trulioo (Ashby) | sponsorship now / only later / never | OPT: "now"; a citizen's "never" never chosen | read by when |
| Trulioo (Ashby) | "legally eligible to work in Canada or the USA?" (Canadian) | "not eligible in Canada or the USA" | "…eligible to work in Canada" |
| Barnes & Thornburg (Ashby) | "prevented from lawfully becoming employed in the US…?" (OPT) | Yes | No |
| Barnes & Thornburg (Ashby) | Law School, Undergraduate School, Graduate School, Graduate Major | the first school for each | that degree's school, or blank |
| Replit (Ashby) | Foster City HQ 3 days a week (non-movers elsewhere) | Yes | No |
| Perchwell, Lyft, Verkada, Klaviyo | graduation periods | another month's (June 2026 took December 2026) | the month's own |
| Exegy (Ashby) | notice-period list | the days to a start date | the notice stated |
| Float, A Thinking Ape | salary in CAD boxes (a Denver resident's 85000) | written | blank |
| Gecko, Reflect Orbital, Saronic, Cowboy Space, Snowflake, Woven, Shield AI | U.S. person, in other wordings and lists | blank | Yes for a citizen or permanent resident, No / Other otherwise |
| Wattpad (Lever) | "Do you require workplace accommodations due to a mental health condition? (Whether work from home or in-office)" | **Yes from everyone willing to move** (read as an office requirement) | No for those who state no disability; blank otherwise, kept from the AI |
| Wattpad (Lever) | accommodations "due to a physical disability" (a persona with a disability) | Yes (from the disability status) | blank, theirs |
| Data Lab (Lever) | "Are you local to the Germantown, MD office (within 25 miles)" | Yes for all | No for all |
| SEP (Lever) | "Does your work authorization now, or will it in the future, involve CPT or OPT?" (H-1B; US citizen) | Yes | No |
| SEP (Lever) | sponsorship options (OPT; citizen) | "I require sponsorship … at this time"; blank | "…not right now, but will…"; "I do not and will not…" |
| SEP (Lever) | "what city will you be working from?" (movers) | their current city | blank |
| eqbank (Lever) | "racialized person?" (Black, Asian personas) | No (read as the Hispanic question) | Yes |
| Spotify (Lever) | a UK ethnicity list (German, French-Canadian; Black; Asian) | "White: Irish"; "Black/Black British: African"; blank | the group's "Any other …" option |
| Palantir (Lever) | university, a University of Washington graduate | Washington College | blank (listed per campus) |
| National Journal (Lever) | "At which institution did you earn your highest degree?" | The Master's College | University of Washington / Northeastern University |
| Fullscript (Lever) | experience "…implementing RBAC and MFA…" | Yes for master's holders, No for others (MFA, a Master of Fine Arts) | blank |
| Data Lab (Lever) | experience with Direct Marketing Campaigns | No for all (a marketing opt-out) | blank |
| Wintermute (Lever) | years in desktop or IT support roles (software careers) | 1-2 years; 5+ years | blank |
| FiscalNote (Lever) | notice list (3 months; 4 weeks; immediately) | 3-4 weeks; 4+ weeks; blank | 4+ weeks; 3-4 weeks; None/Immediate |
| Kobie (Lever) | located in one of these states (Colorado, Massachusetts residents) | No ("Georgia" read as the country) | Yes |
| Artera (Lever) | "Were you referred…? If so, please provide their first and last name." | the applicant's own name | blank |
| Zoox (Lever) | "I am aware that this is a hybrid role… Foster City, CA." (non-movers) | No | Yes |
| Zoox (Lever) | preferred first and last name ("…you do not need to respond") | the first name | blank |
| ERG (Lever) | "Legal Full Name (First Name, Middle Name & Last Name)" | the first name, then blank | the full name |
| Match Group (Lever) | how heard (stated Referral) | Employee Presentation | Other |
| Immuta (Lever) | "If you answered Yes or Unsure… details" (citizen) | US Citizen | blank (the status for those who need sponsorship) |
| Crest, FiscalNote, Riot | a whole address | the city line, or no country | the whole address, with the country abroad |
| Larian (Lever) | on site at a studio the page does not name (a non-mover) | Yes | blank |
| Renaissance (Greenhouse bank 3) | "referred by a current employee of the company?" (stated Referral) | No (since round 4) | Yes |
| UASI (Greenhouse bank 2) | "Do you live locally in Cincinnati, OH and…" (movers) | Yes | No |

## 5. Blanks now answered

- **Every Yes/No question on Ashby**, and Ashby's later form sections.
- Places: "Where are you currently located?"; "Where do you currently live?"
  over states and countries (Squarespace); "Country - Region" lists
  (Dropbox, Intercom; never "US - Washington, D.C" for Washington state);
  a "not listed" option when neither state nor country is listed; a state
  list in the label (Kobie); "able to be located in Jacksonville, FL" for
  movers (RF Smart); Artera's live-there / move / neither.
- "Current or most recent employer", "Where have you most recently
  worked?" (a job that has ended); visa status lists by the applicant's own
  visa (Base Power, Vital Lyfe, OnePay, Doximity; Waymo's for an OPT
  holder).
- Enrollment at a named place for someone enrolled nowhere: "Are you
  currently attending a college or university in the US?" (Superhuman),
  "…a university in Canada?" (Lyft): No for graduates.
- How you heard: "Where did you first see this position?", "How'd you
  hear…", "From which job site did you see this posting?" (Data Lab).
- Employers: "filed an application with us before?", "previous employee of
  any of the Crest family of companies", "Do you currently work for
  Gopuff?", Copper River's "Family of Companies".
- Start dates: "On what date would you be available to work"; "require
  sponorship" (sic) is sponsorship.
- EEO: "I am not a U.S. veteran" (Gopuff); "Caucasian" (AltaML); "Cisgender
  Woman" / "Transgender Man" options (AltaML); race lists named only by
  their options (Wattpad, Faire, Doximity); "racialized person" (eqbank);
  Wattpad's minority list.
- Sponsorship details for those who need sponsorship (Immuta, Calm).
- Kobie's attention check by place ("select the SECOND option"), its
  periodic travel for someone who prefers remote work, Canonical's yearly
  meetups for the same.
- Dayforce's Preferred Contact Method.

## 6. Decisions (one rule each, each easy to reverse)

29. An open menu's "hide others" marks (`data-aria-hidden`) never hide a
    field.
30. A box still showing exactly the page's location guess (Workable's
    `prefilledByLocation`) is empty to the fill.
31. Today's date is written in the box's own format; with none shown,
    month first.
32. A bare salary figure is in the applicant's own currency.
33. A form's later sections of the same kind are part of the form.
34. A school's kind is part of its name ("University of X" is not "X
    College"); a school listed only per campus is never "not listed", and
    the campus is the applicant's.
35. Needing an accommodation (interview or workplace) is No only for
    someone who states no disability; anyone else answers it, never the AI.
36. A visible minority ("racialized person") is any stated race but White;
    Indigenous peoples and two or more races are not decided; "not a
    minority" is never chosen beside "nonvisible minority" for someone
    LGBTQ+ or with a disability.
37. A grouped ethnicity list ("White: Irish") takes the stated group's "any
    other" option, never a subgroup nobody stated.
38. A gender list with cis / trans options takes both stated facts only
    when the stated gender is not offered plain ("Male" beside "Transgender
    Male" is "Male").
39. A first-person statement of awareness over Yes / No is Yes, unless it
    also promises something.
40. Where one will work from is not where one lives now; on site at a place
    nobody named, for someone who will not move, is theirs.
41. Paying for one's own housing or move is the applicant's to answer, like
    relocation assistance.
42. Who completed the application, or whether an AI helped: never answered,
    never sent to the AI (round 4's decision 6, now on the device).
43. Kept: No to non-competes (your day-session decision 1, "unencumbered"),
    also where "other restrictive covenants" are named. This sits uneasily
    with decision 27 (NDAs are common); say if those should be yours.
44. A graduation term in winter is never chosen from the year alone (an
    April 2027 graduate is not "Winter 2027").
45. "Pursuing a degree in X or a related field": a degree in progress
    outside the sciences and engineering is the applicant's to judge.
46. Working for a partner, vendor, client or competitor of the company is
    not working for the company.
47. A visa a country's citizens may use ("TN (Applicable for citizens of
    Canada or Mexico)") is not the citizen's status; an option that names
    the applicant by citizenship ("Canadian or Mexican Citizen (TN Visa)")
    is.
48. Someone enrolled nowhere is not enrolled at a named place; where a
    student's school is, the profile does not say, so that stays theirs.
49. Kept (an earlier round's rule, newly visible on Ashby's Yes/No): a
    posting's own requirement is accepted by applying unless the profile
    refuses it: relocating to New York for Ramp's internship, on site in
    Rochester for MegaZone, travel as needed for NPX, all Yes for a profile
    that says nothing about moving. An open-ended "Are you open to
    relocation?" stays blank without a stated answer. Say if a silent
    profile should leave requirements blank too.
50. A web component's own `label` attribute names its field when nothing
    else does (SmartRecruiters' `<spl-input label="First name">`).
51. Agreeing to arbitrate, or waiving a jury trial or a class action, is the
    applicant's alone: left blank, never sent to the AI (Anthropic, Roblox,
    Asana, Block, sweetgreen). Receiving such an agreement or reading it
    gives nothing up and stays answered (Coinbase, Anthropic's "I will
    read"), and so does an application certification, release for
    reference checks and at-will clause included (National Journal):
    nobody can apply without it.

## 7. Regression

Every pinned live case (201), in 16 batches of 13, headful, on the build of
c144207 (04:48): **168 passed, 33 failed.** Every failure was read:

| cause | cases | what now |
| --- | --- | --- |
| the posting closed (Greenhouse's job API 404: Robinhood x3, OneImaging x2, D2L, Duolingo; Ashby's API no longer lists it: Greenboard, NTT Data; Pinpoint redirects home: IDT; JazzHR 410 Gone: Directors Investment Group) | 11 (8 postings) | retired, each with a comment saying why |
| the git-ignored sample PDF was missing from the worktree | 1 | copied in; passes |
| answers the cases never had: Ashby's Yes/No questions, Lever survey questions now labelled, Workable's Address now the profile's | 11 | each read against its persona, then pinned |
| wrong answers | 7 (5 bugs) | fixed, a failing test first: both of Superhuman's education rows took Concordia; Waymo's "If yes, what kind?" took TN; Ramp's degree question (3 cases); Grow Therapy's winter term; The Exploration Company's own housing |
| the page changed: Astranis reworded its SMS consent | 1 | re-pinned (still No) |
| Databricks' Degree dropdown did not open once | 1 | passed on re-run (25/25) |
| SmartRecruiters ServiceNow read every label as its id; City stayed blank | 1 | fallback added, not verified live (section 8) |

(Grow Therapy also renamed its custom fields' ids and Waymo's phone widget
stopped adding +1: both re-pinned.)

Then every failing case still open, every round-5 page and every new pin
were re-run on the fixed build in six runs of 13 or fewer (r5re-A..F) and
read: **46 distinct pinned cases, every one passing** once its new answers
were read and pinned (the 13 round-5 Ashby pages, 12 of the 13 Lever pages,
and the 21 regression failures still open, all but ServiceNow).

**The final regression** (190 pinned cases after the retirements, 15
batches of 13, on the build with every fix through 1161e4b): **189
passed.** The one failure was SEP's "Current location": Lever's search was
slow and the adapter gave up after 4 s, as on Wattpad once earlier. Fixed
(it waits 8 s); both pages pass since. ServiceNow passed 12/12.

Three fixes landed after it (the Lever wait; iCIMS's and L3Harris' Apply
wording), so everything they touch was re-run on the last build: SEP and
Wattpad, and **all 19 pinned cases that enter through an Apply button**
(BambooHR, Rippling, Dayforce, Breezy, Pinpoint, Oracle, Recruitee,
Paylocity, Jobvite, Epic): every one passes.

Before the push to main, CI's checks ran locally, on the branch and again
merged with main (26f6310: the jobs right rail and the local match
scorer): backend pytest on SQLite, 2,749 passed on the branch and 2,777
merged; extension typecheck, build, scan-smoke and 1,979 unit tests (1
skipped: the opt-in question bank); frontend tsc and vite build.

## 8. Decided, and what still needs a person

**Decided (you answered, or asked me to choose; 2026-10-08)**
1. **No new Store zip for now** (yours). 0.5.0 stays unuploaded and
   predates round 5.
2. **`EXTENSION_ALLOWED_IDS` is set on Vercel** (yours): Store installs
   can connect.
3. **Legal waivers:** decision 51. Arbitration agreements and jury or
   class-action waivers are left blank and kept from the AI; application
   certifications stay accepted. Anthropic's three pinned pages re-run
   live with the agreement blank: all pass.
4. **OpenAI:** no change. Nothing tonight needed it; what is left to the
   AI (essays, "May we contact your employer?", Barnes' degree types)
   stays blank while billing is down and fills again once it works.
5. **The two Workable fixtures:** scrubbed (0649b67); the git history
   still holds the old value.
6. **A bare salary figure is the home currency:** kept (decision 32).
7. **The brief's cut-off first P3 item:** unknown; dropped.
8. **A posting's requirements for a silent profile:** kept (decision 49).
   Applying for the posting says as much, a stated "willing to relocate:
   No" still overrides it, and the applicant reviews every page before
   submitting.
9. **SuccessFactors' POST:** stays blocked. Live runs send no POST to an
   employer's site whose effect I cannot vouch for; SuccessFactors' form
   is a manual check.

**Not verified live, or still open**
- **ServiceNow on SmartRecruiters** (the suite's one SmartRecruiters
  page): it passed 12/12 in the final regression, but with its labels
  read the ordinary way ("First name*"), so the fallback that reads a web
  component's own `label` attribute (1161e4b) never fired. It is
  unit-tested (the test reproduces the regression's ids exactly), not
  verified live; SmartRecruiters served a DataDome CAPTCHA to both of my
  probes in between.
- **Wattpad's location typeahead and Databricks' Degree dropdown** each
  missed once tonight and passed on every other run: page timing, most
  likely. Watch them in the next regression.
- **Waymo's phone widget** stopped adding +1 tonight; the pin now accepts
  either form. The value written is the same as before.
- **OneImaging's embed host page** (`real-live-embedded.mjs`) has no open
  posting to frame; Waymo still runs a live Greenhouse embed. Point it at
  an open posting to restore the second embed case.
- **Not built:** Jobvite and SmartRecruiters question banks (section 3).
  **Not explored:** Taleo (no active Taleo posting in prod `scraped_jobs`
  tonight).
- **iCIMS job pages:** the flow needs to look for the Apply button in a
  same-origin child frame too (every iCIMS posting tonight); behind it is
  most likely a candidate login (an account wall). Not built tonight; the
  wording fix (212f0c5) is unit-tested only.
- **SuccessFactors:** the Apply click now lands (L3Harris, Acuity), but the
  next step is a POST that live runs block, so its form stays unexplored.

# Round 4: real users first, then new ground (2026-10-04, night)

Branch `night/autofill-round4`, from main at 775ff09. Local only: NOT pushed,
NOT deployed. Nothing was submitted anywhere: every live run blocks non-GET
requests (GraphQL queries aside), and no account was created on a real site.

## 1. Can Web Store users connect? And a release to upload

**Connecting.** I could not read the Vercel setting myself (the Vercel
connector answers 403 for the team that owns the `resumate` project), so I
checked what the setting does instead. Prod `security_events` shows completed
extension handshakes from the **Store** id (`dadbhjlflnljgailcpgehdainjdmjeej`)
on 2026-07-14, 08-07, 09-14 and 09-28, and no rejected redirect since
2026-06-27. Store installs can connect today. Please still look at
`EXTENSION_ALLOWED_IDS` on the dashboard (it should hold both ids,
`apgogjfdpleeajnngkfkfekbddcpodkl,dadbhjlflnljgailcpgehdainjdmjeej`); I did
not change any Vercel setting.

**Release 0.5.0, ready to upload (I did not upload it).** The Store still
serves 0.4.0 from July 15 (3 users), and every real fill in prod telemetry
since then ran on it. `chrome-extension/tailrd-extension-0.5.0.zip`, built
from commit 31a4371 after everything in section 8 (231 KB; checked: 8 files, no dev URLs, no key). What to do, the release notes and two listing lines that
are now out of date are in `docs/store-submission.md` ("Release 0.5.0").

**Real fills since 2026-10-04:** none in prod `autofill_reports`. The last
real ones (2026-09-28, 10-03) ran the July build; what they got wrong was
re-run on the current build first (batch R below).

## 2. How it was measured

Each batch was run blind on the build before its fixes, every write read with
`tools/review.cjs`, then fixed, re-run live and pinned.

| batch | pages | blind run: wrong values | after |
| --- | --- | --- | --- |
| R: the real users' pages (Greenhouse embeds) | 4 | 1, on 1 page (Waymo) | 0; pinned |
| 1: SmartRecruiters, JazzHR, Breezy, Workable | 5 usable (SmartRecruiters' DataDome blocks every page after the first) | 8, all on the JazzHR page | 0; pinned |
| 2: Greenhouse, 13 companies on their own career sites | 13 | 5, on 4 pages | 0; pinned |

Then a **question bank**: the public Greenhouse API serves every posting's
questions, so 80 postings (806 distinct questions) were rendered as forms and
answered for six personas at once (`test/qbank.test.ts`, opt-in with `QBANK`).
Reading 4,800 answers that way found far more than live pages could: about 40
wrong answers, each fixed with a test. It is code-level (no live widget), so
anything it found was also checked by a unit test and, where the page was in a
batch, live.

New personas (synthetic, `test/e2e/profiles.mjs`): a US worker on H-1B, one on
STEM OPT, a bootcamp graduate with a career gap and a disability, a Berlin
engineer (German Diplom, transgender, bisexual), and an Indian new graduate.

## 3. Wrong answers fixed

Live pages (the value written, and now):

| page | question | wrote | now |
| --- | --- | --- | --- |
| Waymo (real-user page) | Do you require work authorization? (Canadian, US job) | No | Yes |
| JazzHR, Directors Investment Group | misrepresentation statement (I consent / I do not Consent) | I do not Consent | I consent |
| JazzHR | second Institution Name and Major | the first school's | the second school's |
| JazzHR | second Employer Name | the current employer | the previous one |
| JazzHR | each employer's Address (x2) | the applicant's street | blank |
| JazzHR | each school's Location (x2) | the applicant's city | blank |
| Duolingo | "After the OPT, eligible for a 24-month OPT extension?" (US citizen) | Yes | No |
| Duolingo | Alternate Email | the same email | blank |
| Coveo | member of the 2SLGBTQI+ community? (bisexual, transgender) | Prefer not to say | Yes / Oui |
| Cloudflare | Legal Name (if different than above) | the same name | blank |
| Accenture Federal | Degree, the high-school row | Technical Diploma | High School Diploma/GED |
| Epic Games | Degree, the high-school row | (unseen) | High School / Secondary Education |

Question bank (each also a unit test):

| company | question | wrote | now |
| --- | --- | --- | --- |
| Sony Music, DoorDash | "…require sponsorship (e.g., H-1B, E-3, TN, O-1, STEM OPT…)?" for applicants needing it | No | Yes |
| Toast | "Do you now, or will you ever, require sponsorship?" (OPT) | No | Yes |
| Datadog | authorized to work here? (Berlin, India) | Yes, but I will need sponsorship | No, I need sponsorship now |
| Peloton | "By selecting Yes you confirm you do not require sponsorship" | the inverse, for everyone | the statement |
| Peloton | able to commute to the NYC HQ "(located at …)" | No for everyone (the office's address read as the applicant's) | Yes if moving, No if not |
| Nuro | 4 days a week "in our Mountain View, CA headquarters" (Seattle, will not move) | Yes | No |
| Sentinel (JazzHR) | "work out of our Wakefield, MA office 1 day per week" (Austin, will not move) | Yes | No |
| Relativity | "willing to commute and/or relocate? If not, explain" (text) | the applicant's city | Yes / blank |
| Squarespace | "Do you plan to move out of your state in 6-12 months?" | the current state | "I have no plans to move" / blank |
| Squarespace | most recently attended school (Turing School) | Parsons School of Design | blank |
| Waymo | state/region of residence (Berlin, India) | Other | EMEA, APAC |
| Samsara | enrolled in a bachelor's or graduated in the past 2 years (June 2026 graduate) | No | Yes |
| Stripe | "When do you expect to complete your degree?" | the degree's name | April 2027 / blank |
| Canonical | bachelor's degree result, with its grading system | the school and degree | 3.7/4.0 / blank |
| Duolingo | Undergraduate GPA (her GPA is the master's) | 3.85 | blank |
| Jane Street | Secondary Major / University Email Address | the major / the personal email | blank |
| K1 (JazzHR) | undergraduate graduation year | 2016 (the master's) | 2015 |
| Glossier | days you cannot work | the start date | blank |
| StackAdapt | salary expectations (hourly) | 185,000 | blank |
| Nuro | U.S. driver's license held 3 consecutive years (Toronto, Berlin) | Yes | blank |
| Bandwidth | how did you hear (stated "Company website") | NSBE's career site | Other |
| Twilio, Gusto, Coinbase… | how did you hear (stated "Social media") | Twitter / Facebook | Other / blank |
| Block | signature box (type your full name) | the last name | the full name |
| Pinterest | "…please list the type of support you may require" | Yes / No | blank |
| Affirm | how did you hear (stated "Job board") | Affirm's Career Site | the job board |
| AlayaCare | years of fullstack experience (Python/React) | the career total | blank |
| Sweetgreen | consent to the video recording itself | I consent | blank (yours) |
| GitLab | sponsorship to remain in your location (Berlin citizen) | Yes | No |

**False failures** (written right, reported wrong): Waymo's phone "did not
stick" (read back as +14165550142), Greenhouse's "+1" reverted, Duolingo's
13 "didn't stick" (its widgets read back), Workday's month/range/veteran
"reverts", a running job's To date, Epic's Country.

## 4. Blanks now answered

- **Epic Games**: 16 dropdowns built on an older react-select (no combobox
  role) were invisible, then unlabelled; work authorization, sponsorship, how
  you heard, 40 hours a week, two acknowledgements, School/Degree/Discipline.
  13 fields filled before, 23 now.
- **Job's country on company career sites**: "Location" label/value pairs
  (Epic), the line under the job title (Databricks). Without it, work-right
  and sponsorship questions stayed blank.
- Seattle's city on Greenhouse (the lookup lists it twice); GitLab's visa
  list ("Yes, but not one of the visas listed here", "Yes, F-1 Visa OPT");
  Airbnb's "Yes … now / Yes … in the future".
- Export-control lists of embargoed places: "None of the above" when none of
  the applicant's countries is listed (Databricks).
- Graduates: "Earlier than Fall 2026", "Prior to December 2025", "Already
  graduated", "N/A (I have graduated already)".
- Start dates: onboarding date, start year, "available to begin before
  September 2028".
- Acknowledgements: Coinbase's AI notice, OneTrust/Samsara "Acknowledge/
  Confirm", Riot's E-Verify notice for everyone (a "Select..." placeholder
  had counted as an option).
- EEO in other words: "Black / Of African descent", a cisgender woman among
  transgender-only qualified options.

## 5. Decisions (one rule each, each easy to reverse)

1. A label that lists the applicant's own status as sponsorship ("e.g., …
   STEM OPT") makes "now" Yes; otherwise an OPT/EAD holder needs it later,
   not now.
2. A visa list: the applicant's visa, else "not listed"; no visa stated, blank.
3. A school is matched by its own words; no match leaves it blank (never
   "My school is not listed").
4. The profile's one GPA belongs to its main degree: blank for an
   undergraduate GPA when the main degree is a master's.
5. Plans to move: "no plans" only for someone who will not relocate.
6. Pledges about your own words or no AI use (Canonical, Twilio) stay blank:
   Tailrd may have written the words.
7. Consent to being recorded stays yours even when it is the only option.
8. Embargoed-place lists: answered only when every fact the question asks
   about is known and none of your countries is listed.
9. "Social media" is no particular platform; "Company website" is only the
   company's own site.
10. Old react-select widgets (aria-autocomplete="list", emotion class names)
    are dropdowns.

## 6. Regression

All 157 pinned pages from rounds 1 to 4, headful, in 13 batches (build of
bf9c7fc): 143 passed. The other 14 were re-run on the final build:

- 8 passed: four fixed during the run (Figma's "Other", Shield AI's "local
  to or willing to relocate", Hermeus' U.S.-person option, and hCaptcha's own
  frame showing up in the page dump), four were page-load flakes (GitAI,
  Figma, Mindex, Waymo).
- 5 now answer questions they used to leave blank; each answer was read and
  then pinned: Sentinel (No to the Wakefield office from Austin, Yes to the
  US work right), K1 (undergraduate year 2015, LinkedIn, the commute, Southern
  California), D2L (eligible in Canada), DoorDash ("Before December 2027").
  Commvault's channel for a stated "Job board" is now blank (it was the
  company's career page).
- 1, Bosch on SmartRecruiters, now sits behind a Cloudflare challenge that
  needs a POST; live runs block it.

Also on the final build: unit tests 1713/1713, type check clean, scan smoke
passed, and every multi-page probe: Workday one click to Review 73/73, the
generic multi-page flow, Workday account creation and its gate, the
react-select driver, Workday churn.

## 7. Needs you / needs manual verification

**Your decisions**
1. **Upload 0.5.0** (`tailrd-extension-0.5.0.zip`) and consider the two
   listing lines in `docs/store-submission.md`. I did not upload anything.
2. **Look at `EXTENSION_ALLOWED_IDS` on Vercel** (both ids); the evidence
   says it works, but I could not read it.
3. **Legal waivers are inconsistent.** Anthropic's and Roblox's arbitration
   agreements are accepted (round 3's decision 8: a lone acknowledgement is
   answered); Block's (a lone "Accept" checkbox) and Sweetgreen's ("Have you
   read and do you agree to the Arbitration Agreement?" with a lone Yes) stay
   blank. Say which you want for all of them.
4. **Does OpenAI work now?** Prod shows no AI output since 2026-08-25
   (`job_match_scores`). If it does, I can find a safe way to run real
   `/api/fill` answers in the lab; none of this round used the AI.

**Not verified live**
- School and Degree on Epic go to the AI with the real options (Turing
  School and "Certificate" are not in Greenhouse's lists).
- Breezy's work-history rows ("Start dateEnd dateDelete" label) need a page
  capture.
- Airbnb's career site (no form found) and AlayaCare (its Apply button times
  out in the harness).
- SmartRecruiters past the first page (DataDome, not allowed to bypass).

## 8. After the report: batches 3 and 4, four question banks (2026-10-05, early morning)

**Batch 3, the families seen least** (13 postings from prod `scraped_jobs`,
7 personas). Blind run first, then fixes, then two more live runs.

| family | page | reached the form? | wrong values (blind) | now |
| --- | --- | --- | --- | --- |
| Rippling | 4AG Robotics (Berlin persona) | yes | phone: "493012345678" under the page's "+44" | "+49 DE" and "3012345678" |
| Recruitee | Huawei Canada | no: the form is in a hidden "Apply" tab, and three of its answers were filled unseen | 0 | opens the tab; name, email, phone |
| Paylocity | Choice Solutions | no: the email check is a POST | 0 | opens (the email check let through, nothing else); 21 fields right; State stays blank (below) |
| Jobvite | ACPM (French page) | no: "Poser sa candidature" | 0 | opens; 6 fields, all right |
| Dayforce | Eclipse | no: Apply's aria-label names the job; then a guest chooser | 0 | opens as a guest; 21 fields right after the fixes: Country, State and both dial codes were set but read as failed, How did you hear and Current Job were never seen |
| BambooHR | NextHop | yes | 0 | 11 fields, all right |
| CareerPuck (Greenhouse) | Domino | yes | 0 | 16 fields, all right |
| Breezy | Ninja Holdings | yes | 0 | 6 fields; the Chicago hybrid question now answered |
| Pinpoint | Franklin Electric | yes | 0 | 16 fields, all right |

Not cases (each stops before any field): ADP (sign-in), Avature (account
creation), Eightfold (only a job-alert signup on the posting), Acuity's
SuccessFactors (a menu leading to an SAP sign-in).

**Question banks 2 and 3**: 79 and then 74 more Greenhouse postings (bank 3
from boards our users saw that no bank had yet), 6 personas, about 9,000
answers more. Read the same way as bank 1; all banks re-run after every fix
and every changed answer read.

**Question bank 4: Workable.** Workable's public form endpoint serves every
posting's custom questions, so 89 postings from the 31 Workable accounts in
prod `scraped_jobs` (821 questions) were answered for the same six
personas and every answer read. It found the worst answers of the night:
three adults saying they were under 18, a first-person "I am authorized and
will not need sponsorship" answered backwards for everyone, a gender
identity typed into an EEO policy's name box, and a certification declined
for anyone who declines to state a disability. About 30 wrong answers, each
fixed with a failing test first (`test/bankWorkable.test.ts`).

**Batch 4: the bank's pages, live.** 11 of those Workable postings, each with
the persona that got a question wrong, run blind on the build with the
fixes, every write read: each fix it touched held on the real widgets. One
wrong value was written (No into Credence's at-will notice, "Initial to
agree."), and the live runs found two bugs the bank could not: every
Workable Yes/No question was reported "No option matches" beside the
native twin that filled it, and Saalex's tab froze (below). All three
fixed, then pinned: 11 pages, 279 checks.

### Wrong answers fixed (after the report)

| page | question | wrote | now |
| --- | --- | --- | --- |
| Rippling (live) | phone, beside a dial-code picker preset to +44 | 493012345678 under +44 | +49 DE, 3012345678 |
| Paylocity (live) | how did you hear (stated "Company website") | Other, "Company website" typed in its box | the company's own "Choice Website" (the company name is read now) |
| K2 Space | relocation to Los Angeles (no applicant lives there) | "I live in the greater Los Angeles area" | "Yes, I'm willing to relocate" / "No, I'm unable to relocate" |
| Anduril | "If you are not local to Colorado…" (Berlin, Bengaluru) | local | not local |
| Accenture Federal | how did you hear (stated "Referral") | Former Employee | Connection in the Company |
| Datacor | authorized to work in "the location you currently reside" (Canadian, German, Indian at home) | No (and sponsorship Yes) | Yes (No) |
| Datacor, DV Trading, RF Smart | the university you are currently enrolled in / attend (graduates) | their old school | blank |
| Datacor | "what degree you are currently pursuing, with majors" | the major (graduates too) | the degree in progress; graduates blank |
| DoorDash | "at least 1 internship or relevant full-time experience?" (staff engineers) | No | Yes |
| DoorDash, Klaviyo | GPA 8.6/10 against a 4.0 list | 3.75+ / 4+ | blank |
| DV Trading | Undergrad Discipline(s) (master's holders) | the master's field | the bachelor's field |
| Cloudflare | "a student in F-1 status planning CPT/OPT?" (Canadian citizen) | Yes | No (and an OPT holder: Yes) |
| Cloudflare, Together AI, DV Trading, Astranis | in-office at "our Austin office", "our San Francisco office", "our Chicago office", "San Francisco HQ" (Seattle, Denver; will not move) | Yes | No |
| Giftogram | "US Citizen or Green Card Holder that can work onsite in Whippany NJ" (Denver citizen, will not move) | Yes | No |
| Covar, GitAI, STR, Tenet3, Varda | US citizen? (F-1 OPT) | blank | No |
| DV Trading | expected graduation (June 2026 graduate) | August 2026 - December 2026 | I've already graduated |
| SharkNinja | Which college did you attend? (long list) | blank | the school, matched by its own words (Turing, IIT Madras, TU Munich stay blank: not listed) |
| Asana, Intercom | citizen of Cuba, Iran, North Korea or Syria? ("U.S. export control laws" in the label; US citizen) | Yes | No |
| Airtable, Everlaw, MyFundedFutures, Terraclear | permanent / unrestricted US work authorization (H-1B, OPT) | Yes | No |
| Warp | permanent authorization "in the U.S. or Canada" (Canadian citizen) | No | Yes |
| Warp | "will you be based in the U.S. or Canada?" (Seattle, Denver; will not move) | No | Yes |
| Elastic | "In what countries do you have the unrestricted right to work?" | the status statement | the countries |
| Airtable | "How are you using AI today in your current role?" | the job title | blank |
| Sysk Hennessy | "Did anyone in refer you to this position?" (text) | the job title | blank |
| Hootsuite, Hudl, Wikimedia, Recidiviz | SaaS sales experience, experience in sport, in product marketing | the whole work history | blank |
| CircleCI | "Are you located in…?", "Are your salary expectations within the band?" (text) | the city, the salary | blank |
| Starburst, Doximity | which US state you will work from (Bengaluru) | Indiana | blank |
| AssemblyAI | "hybrid in NYC, 2 days per week" (Seattle, will not move) | Yes | No |
| Vestmark | "come into the Wakefield, MA office five days" (Seattle, Denver; will not move) | Yes | No |
| Flipp | how did you first hear (stated LinkedIn) | a recruiter reached out | Job board |
| Geotab | how did you hear (stated LinkedIn) | Handshake | blank |
| Samsara, Faire | how did you hear (stated LinkedIn) | an alumni group / an online ad | LinkedIn Jobs / Job posting on LinkedIn |
| CTC | "highest degree level you are currently pursuing" (graduates) | their old degree | blank (the student: Bachelor's) |
| NISC | "most recently completed form of education" (a student) | Bachelor's Degree | blank (graduates: Master's) |
| Renaissance | "If you were referred…, what is the employee's full name?" (referred) | the applicant's own name | blank |
| LaunchDarkly, Blue Moon, Coinbase | referred by an employee? (stated "Referral") | No | Yes |
| Clearway, NISC | salary list in dollars (stated in euros) | $120,000 / 111-120k | blank |
| A Thinking Ape | "desired salary (CAD$)" (stated in USD or EUR) | the other currency | blank |
| Hudl | "What is your preferred office location?" | the home city | blank |
| New Relic (Tokyo) | sponsorship "(e.g. U.S. F-1, H-1B…)" (US citizen) | No (read as a US question) | blank |
| Palantir (regression) | university, Imperial College London | Other - School Not Listed | Imperial College London - ICL |
| Anduril, Planet, MongoDB (regression) | how did you hear (stated "Company website") | Other / blank | the company's own careers site |
| Dayforce (live) | Country, State/Province, both dial codes | set, then reported "Selection didn't stick" | read from what the select shows |
| Workable: Saalex, Credence (bank 4, live) | "Are you at least 18…? (If no, you may be required to provide authorization to work)" (Canadian, German, Indian adults) | No | Yes |
| Workable: DISA (bank 4, live) | "I am legally authorized to work in the United States and will not require visa sponsorship now or in the future." | Yes for everyone who needs sponsorship, No for the citizen | Yes for the citizen only |
| Workable: OnLogic (bank 4, live) | able to work in the US without employment visa sponsorship, now / in the future (H-1B; OPT) | Yes / Yes | H-1B No / No; OPT Yes / No |
| Workable: Credence (bank 4, live) | "authorized to work in the U.S. as a U.S. citizen…?" (H-1B, OPT) | Yes | No |
| Workable: Open Data Jobs (bank 4) | "Check every country of which you are a citizen." (H-1B, OPT, living in the US) | United States of America | blank (a German, an Indian: Other) |
| Workable: Flourish Research (bank 4, live) | EEO policy ending "To acknowledge, please enter your full name below." | Cisgender / Transgender | the name |
| Workable: SSCI (bank 4, live) | certification, Accept or Decline (applicants who decline to state a disability) | Decline | Accept |
| Workable: Credence (bank 4, live) | certification "…Initials to agree"; an at-will notice "…Initial to agree." | the current employer; No | blank |
| Workable: Saalex (bank 4, live) | "I affirm that I have not entered into any non-competition, non-disclosure… agreement" | No | blank |
| Workable: Credence, Avalore (bank 4) | a bachelor's degree minimum (a student, a bootcamp graduate) | Yes | No |
| Workable: Saalex (bank 4, live) | "Do you have a HS Diploma or GED?" (a university student) | No | Yes |
| Workable: RentVision (bank 4, live) | "At which university will you be enrolled for the Fall 2027 semester?" (graduates; a student finishing in April 2027) | their school | blank |
| Workable: Open Data Jobs (bank 4, live) | undergraduate major and institution (master's holders) | the master's | the bachelor's |
| Workable: Saalex (bank 4, live) | "Highschool Name & Location:", "Supervisor Name & Title:" | the city they live in; their own title | blank |
| Workable: Saalex, JeffreyM, Smartflower (bank 4, live) | years doing one thing (Windows Server administration, owning customer implementations, servicing industrial equipment) | the career total (Yes, 15) | blank (No when the whole dated career is shorter) |
| Workable: Saalex (bank 4) | "experience managing program risks… cost, schedule…" | Yes for everyone | blank |
| Workable: Financeit (bank 4, live) | "comfortable commuting to our office 2-3x a week in Downtown Toronto?" (Seattle, Denver; will not move) | Yes | No |
| Workable: RentVision (bank 4, live) | relocate to Lincoln "(Please check YES if you already live in the Lincoln, NE area.)" (willing to move) | No | Yes |
| Workable: Credence (bank 4, live) | "currently meet the worksite location requirements" (a job in Korea, from Toronto) | Yes | No |
| Workable: Mindex, Financeit, Enfos (bank 4) | salary in a number box (stated "€120.000") | 120 | 120000 |
| Workable: United Placement Group (bank 4) | "How would you rate your closing skills?"; a link to "a project you built with an LLM API" | the skills list; the GitHub profile | blank |
| Workable (live) | every Yes/No question | filled, and reported "No option matches" beside it | reported once, filled |
| Workable: Saalex (live) | the whole page | the tab froze: choosing the State after the date boxes were typed sent Workable's form into an endless re-render (a US applicant on the previous build too) | date boxes typed last; the page finishes in about 23 s |
| Gas South (Greenhouse bank 2) | ADA statement: "Please select Yes to confirm that you have read this statement" | the disability answer | blank |
| Podium, UASI (Greenhouse bank 2) | years providing product support, analyzing data (a data engineer, a bootcamp graduate) | the career total | blank |
| Lodestar Space (Greenhouse bank 3) | "Do you consent to Lodestar Space requesting information regarding citizenship…?" | No for non-citizens | blank |

### Blanks now answered (after the report)

- Apply buttons: by what they show (Dayforce's reads "Apply", its aria-label
  names the job), in French ("Poser sa candidature", "Candidater"), and the
  guest path beside a sign-in ("Apply without an Account").
- Phones: a dial-code picker is set to the number's own country; a box the
  page starts with "+1" is filled; a virtualized list (7 of 245 options
  rendered) is no longer read as the whole list; a filter that answers after
  a half-second debounce is waited for.
- Paylocity: its company name (read from the page).
- Rippling's Location (Google Places): a place search waits up to 4 s.
- "Have you worked with us before?", "Are you graduating in Spring 2027?",
  "Which college did you attend?", "…currently attending / did you graduate
  from?".
- Dayforce: its selects without a search box (How did you hear: LinkedIN),
  and Current Job ticked for the job still running.
- Workable bank: a LinkedIn or GitHub link asked for at length in a text
  area; "eligibility to work", "eligible to be employed", "file a petition
  … for employment-based status"; "Are you a U.S. person?"; French
  "Résidez-vous…" and "autorisés à occuper un emploi"; "Are you currently
  employed?"; "an internal employee of X"; "Latest/Most Recent Employer";
  "current degree program or highest level"; a clearance list's "I currently
  do not have an active security clearance"; an attention check ("please
  choose option C"); the state you will live and work in (US residents).
- An EU, EEA or Swiss citizen asked about another such country (Austria,
  France, Norway, Denmark): Yes, no sponsorship.

### Decisions (after the report)

11. A phone's dial-code picker takes the country of the number's own code
    (+44 is the United Kingdom wherever you live); the box beside it gets the
    national number. A picker or box showing only a code is the page's
    preset, so it is replaced.
12. "A student in F-1 status planning CPT/OPT?" is a visa question: Yes for
    F-1/OPT/CPT stated, No for any other status stated.
13. "Relevant full-time experience" counts technical titles; non-technical
    ones leave it to you.
14. A GPA on another scale than the form's is left blank (no conversion).
15. The guest path ("Apply without an Account") is always taken over signing
    in; no account is ever created.
16. A legal-status Yes in a question that also requires in-person work
    somewhere you will not move to becomes No.
17. A how/why or yes/no question in a text box never gets a fact its words
    mention (title, city, school, salary); experience of one kind never gets
    the whole work history.
18. "Permanent" or "unrestricted" work authorization is a citizen's or
    permanent resident's; a question naming two countries joined by "or"
    counts either.
19. A stated referral answers "referred by an employee?" Yes; who referred
    you is always left to you (the AI is kept out of it too).
20. A salary written in a currency is never put in a list, or a box, that
    asks for another currency.
21. A sanctioned-countries question is read for the countries it lists,
    never for "U.S. export control laws" around them.
22. An EU, EEA or Swiss citizen is authorized to work in every other such
    country and needs no sponsorship there (freedom of movement). The UK is
    not one of them.
23. A long statement (40 words or more) is read by what it asks: its
    questions, its requests ("Please indicate…"), its last sentence that is
    not a note, and a short title before a dash. What it merely mentions
    (gender identity, disability, a criminal check, an employer) never
    decides the field. A lone "I Agree" still acknowledges the whole text.
24. Experience narrowed by an activity ("performing X", "owning Y") is
    never the career total; when the dated career is shorter than the
    years asked, the answer is No.
25. An attention check that names its option ("choose option C") is
    answered as it says.
26. Another country's list of states or provinces, without the applicant's
    own, is left blank and kept from the AI.
27. An affirmation of a negative ("I affirm that I have not entered into any
    non-compete or NDA") is left to you, never defaulted: most people have
    signed an NDA.
28. A Dayforce work start date stays blank when the profile knows only the
    month (unchanged since 2026-10-03: no invented day).

### Regression (after the report)

Every pinned page, three times, 13 per batch, headful:

- **Regression 1** (build of 4bb117b, 166 pages): 154 passed as scored. Of
  the 12 others, 5 passed when re-run on the next build (MongoDB, Palantir,
  Planet and Anduril with bank 3's company and school fixes; one Workable
  page); 5 answered more than their pins and were re-pinned after each
  answer was read (Rippling's phone code on two pages, Recruitee's "+1"
  box, Breezy's hybrid question, Paylocity's company question); Databricks'
  OneTrust cookie boxes became page-owned (they mount late with the page's
  own defaults; the scanner never touches them); Bosch sat behind a
  Cloudflare challenge.
- **Regression 2** (build of 4bc02f1, 166 pages): 161 passed. ServiceNow sat behind a
  Cloudflare challenge (like Bosch); the other four (Ashby's second school
  search, Databricks' school, CareerPuck's late form, Lever's location
  typeahead) passed when re-run alone on the next build.
- **Regression 3** (build of 31a4371, 177 pages with batch 4): 169 passed
  as scored. Pinpoint IDT now answers its federal-employment disclosure
  (No; read by what it asks) and two page-owned values appeared (Recruitee's
  language switcher, DISA's address that Workable fills from the visitor's
  location): read, then pinned. Astranis' Discipline and Rippling 4AG's
  Google Places location passed re-run alone. Open: Bosch (Cloudflare),
  BambooHR Armstrong (its form never loaded, twice), Ashby Superhuman's
  second school (below).

### Needs you / not verified (after the report)

- **The harness browser takes this machine's locale**: Pinpoint presets
  Country to Canada and Rippling its phone code to +44 for every persona.
  With a US locale Pinpoint presets the US. Real users get their own; the
  extension never overwrites a country already shown.
- Recruitee and Breezy stop at the résumé upload, BambooHR, CareerPuck and
  Jobvite at a captcha: the fields before them are verified, the steps after
  are not.
- **Workable's frozen tab (Saalex).** Paused in the debugger, the loop is
  Workable's own date-format code, re-rendering after the State changed
  with dates already typed. We now type dates last and the page finishes
  (both personas, live). Not verified: whether a person who changes the
  State themselves after the dates are filled freezes it too (a plain
  browser test was blocked by the page's overlays). If a user reports a
  frozen Workable tab, this is the first suspect.
- **Paylocity's own State select stays "Select a state" live.** Its menu
  never opens for us (the unit replica fills; the live widget does not). A
  plain browser could not open it either: a page modal and the cookie
  banner sit over it. It needs a session with the modal closed.
- **Paylocity's "How did you hear" is chosen, and still reported "No option
  matches".** The radios carry plain labels; the cause is not found. The
  page is right; the panel's count of fields needing attention is one too
  many there.
- **Dayforce**: Preferred Contact Method goes to the AI; the required work
  Start Dates stay blank (decision 28); Not Completed and Duties go to the
  AI.
- **Workable fills its Address from the visitor's location** (this
  machine's own city, for every persona) and the extension never overwrites a value
  already there: a real user gets their own city, someone applying from
  elsewhere gets the page's guess. Flourish's Zip box shows the "10119" we
  type as "10,119" (the page's formatting).
- DISA's posting dropped its Education section since it was pinned; its
  education pins now apply only when the section is there.
- **Ashby Superhuman's second school row**: Ashby re-mounts the row while
  the first school is chosen, and the second reports "Field no longer
  found" (regressions 2 and 3, and alone on the final build; it filled on
  the 07:13 build). Timing on Ashby's side; a rescan-and-retry of a stale
  row would fix it. Not done tonight.
- **BambooHR Armstrong** never showed its form in regression 3 or alone:
  the posting may have closed. Check before re-pinning.
- ServiceNow and Bosch on SmartRecruiters are behind Cloudflare challenges
  that need a POST; live runs block them, so they are not verified.

# One Autofill click to the end of a Workday application (2026-10-04, night)

You asked to finish a full Workday application with a single Autofill click:
the next page shows at the bottom of the panel and the flow carries on by
itself, without another Autofill click, to the end. Same branch, still local
only: NOT pushed, NOT deployed. Nothing was submitted anywhere.

**What it does now.** Click Autofill once on the job posting. The flow opens
the application (Apply, then Apply Manually), creates the account with the
email and password saved under Autofill Information > Account creation, and
fills each page. When a page is filled, the panel's bottom button counts down
("Next page in 2s ▶", or "Create Account in 2s ▶" on the account page) and
the page turns by itself. It stops on the Review page and never clicks Submit.

- Press the button to go at once.
- **Pause** (beside it) holds the page: the button becomes "Continue To The
  Next Page ▶" and waits for you. Clicking or typing in the page while it
  counts down holds it the same way. A hold lasts one page.
- A page with a required question still empty waits for you, as before.
- If you turn a page with the site's own button, the flow follows and fills
  the new page.
- Off switch: `flowAutoContinue` in the extension's settings (on by default,
  no panel toggle yet; the e2e harness turns it off).

**How it was tested.** A real Workday application needs an account and real
submissions, both off limits here. So I built a replica of one
(`test/browser/fixtures/workdayReplica.mjs`), served under a real Workday
address so the extension's Workday code runs, with Workday's field names
taken from Workday's own application code (the Capital One page capture in
`test/e2e/results/har/`). It has the posting, the Apply Manually chooser,
Create Account (hidden consent box, live password rules, and the click
overlay Workday puts over that button, whose handler is on the overlay), My
Information, My Experience (rows behind
Add buttons, month/year boxes, search lists, the résumé drop zone),
Application Questions, Voluntary Disclosures, Self Identify and Review, as one
app with one reused footer button, loading skeletons and re-renders. What is
checked is what each page itself registered when it was saved.

`npm run test:workday-flow`, real Chromium, the packaged extension, a US
applicant with three jobs, a degree, EEO answers and a résumé:

| scenario | result |
| --- | --- |
| one click on the posting, then hands off | Review in about 47 s; all six steps saved in order, none rejected; 40 checks on what each page registered (55 answers); Submit never clicked (48/48) |
| Pause on a counting page | held 5 s, Continue turned it, the rest by itself (9/9) |
| clicking into the page during the countdown | held the same way (7/7) |
| the site's own Save and Continue during the countdown | followed, next page filled, nothing skipped or saved twice (9/9) |

**What the replica caught.** These would have stopped or corrupted a real
Workday application. All fixed, each with a test that fails on the old code:

| page | problem | now |
| --- | --- | --- |
| My Information | State (Workday's `countryRegion`) read as the country: no option matched, left empty | "Texas" |
| My Information | Phone Device Type read as the phone number: left empty | "Mobile" |
| My Information | Phone Extension read as the phone number: the number was typed into it | left blank, never guessed (nor asked of the AI) |
| any page with a required radio question | the flow believed answered radio groups were empty and waited ("Have you previously worked for Acme?"); older than this work | fixed |
| My Experience | dates written like "Jun 2012" were typed whole into each box: the Month box read "2012" | month 6, year 2012 |
| My Experience | the education "To (Actual or Expected)" year (`lastYearAttended`) never filled | the graduation year |
| Voluntary Disclosures | ethnicity read as the city ("ethni**city**Dropdown"): left empty | the stated race |
| Self Identify | the date was recognized too weakly to be filled | today's date |
| Self Identify | the disability boxes ("Please check one of the boxes below:") were not recognized as the disability question; "Prefer not to say" found no "I do not want to answer"; an answer with a comma came out twice | "I do not want to answer" |
| Review | no fields on the page, so the flow ended with "No application form found" and never set up Submit tracking | ends at Review; your submit is recorded in the dashboard |

Also fixed: `comboboxEngine.ts` had a literal NUL byte in a string since
August, so git treated it as binary (its diffs read "Binary files differ").

**Your three rules.** Kept, with one refinement to the in-person rule:
occasional visits (team gatherings, offsites, a few trips a year, a quarterly
visit) are not in-person work, so someone who will not relocate no longer
gets No for them. A regular schedule with occasional offsites still counts as
in-person. Enrollment and U.S. clearance rules unchanged.

**Decisions** (each easy to change)
1. The countdown is 2 seconds (`AUTO_ADVANCE_MS` in `flowController.ts`).
2. The account page turns by itself again, with your saved credentials. An
   August change had made it wait for a press; that was not something you
   asked for.
3. A hold (Pause, or touching the page) lasts one page, not the whole run.
4. A press restarts the flow's 10-minute limit, so a page you held while
   reading no longer times out when you press Continue.

**Tests**
- Unit: 1619/1619 (134 files). Type check clean.
- Browser: Workday replica 73/73; the generic multi-page probe (now
  hands-free), the Workday account and account-gate probes, the churn,
  shadow-click and driver probes, extension load: all pass. Browser suite
  19/19.
- Live pinned pages touched by today's shared changes (decline wording,
  disability/veteran groups, every page with a checkbox group): 37 pages and
  the synthetic Workday forms. All pass. Four pins changed on purpose:
  three held-out Lever pages now take the form's decline for an applicant who
  stated no disability answer (as every other EEO question already did;
  "I do not want to answer" was not recognized as a decline), and the
  synthetic Workday form's ethnicity is now answered. Two pages (Workable
  FSSI, Rippling 4AG) missed one field on the first run and passed on a rerun.

**Needs you / manual verification**
- **A real Workday application, end to end.** Not testable here. In
  particular, unverified on real Workday:
  - its live password-rule messages: if they stay on screen once the
    password is valid, the flow reads them as errors and waits on the
    account page for one press;
  - tenants that send a verification email after Create Account: the flow
    stops there; after verifying and signing in, click Autofill again;
  - whether a tenant uses the older ids (`addressSection_countryRegion`,
    `phone-device-type`) or the newer ones (`formField-countryRegion`,
    `formField-phoneType`): both are handled, only the older ones were in the
    replica.
- **The AI path.** These runs use a dead AI, so what the AI writes for the
  fields still sent to it (role descriptions, essays) was not seen.
- **Known noise.** The extension's own report flags some correct Workday
  answers as "reverted": a month box compared with the whole date, a
  dropdown answer compared with the profile's wording ("6" vs "5-7 years").
  The pages hold the right values; only the self-report is wrong. Telemetry
  only, nothing on screen.

# Round 3: new forms, new people (2026-10-03, night)

You asked to keep going and to vary the data, the cases and the forms. Same
branch, still local only: NOT pushed, NOT deployed. Nothing was submitted
(every non-GET request blocked except GraphQL queries and, on Paylocity only,
its read-only email lookup).

**What ran.** Five new synthetic people (in `test/e2e/profiles.mjs`, no real
data) on 33 live postings from prod `scraped_jobs`, chosen so the job and the
applicant disagree in useful ways:

| person | what makes them different |
| --- | --- |
| US veteran, Austin | Army service, an active clearance with no level, will not relocate, prefers remote |
| London senior engineer | UK only, MSc 2016 and BA 2015, transgender woman, bisexual, a stated disability |
| Montreal career changer | French degree names, teacher then developer, non-binary, Hispanic, education listed oldest first |
| Calgary new grad | no work history, declines every EEO question, starts next week |
| US green-card student | permanent resident, December 2027 graduation, a TA job and a past internship |

Forms: JazzHR, Breezy, Recruitee, Pinpoint, Paylocity, Oracle (families the
suite had never seen), Greenhouse forms embedded on company sites (MongoDB,
D2L, Zipline, Brex), Workable, BambooHR, Greenhouse, Ashby, Lever, Jobvite.

**TL;DR.** Reading every write found 20 kinds of wrong answer written on
live pages (several on more than one page), 3 more the code would have given
had the page let it, and about 30 blanks a stated fact settles. All are
fixed, each with a test that fails on the old code, and each fix was
re-checked on its live page. Commits `fbbe0bf` to `3975b2c`; the 33 pages
are now pinned (541 checks).

### Wrong answers found (fixed)

| page | question | wrote | now |
| --- | --- | --- | --- |
| Pinpoint | Address lines, Town, Postcode | the LinkedIn URL (a section's legend labelled every field) | the address parts |
| Pinpoint, Paylocity | Address Line 2 | a copy of line 1 | blank (or the unit, "app. 3") |
| Breezy (Vagaro) | years developing with C# / ASP.NET Core | "3+ years" (six years of teaching) | blank (no such experience stated) |
| Breezy (Vagaro) | race, on an EEO-1 combined list | "Two or more races (not Hispanic or Latino)" for a Hispanic applicant | the Hispanic option |
| Breezy (NinjaHoldings) | expected month and year of graduation | "2027" | "December 2027" |
| Breezy (NinjaHoldings) | "What is your major? Please describe why…" | the major alone, in an essay box | left to the essay path |
| Paylocity | how did you hear | "Online Job Board" for a stated "Career fair" | "Other", with "Career fair" in its box |
| Paylocity | an education row's City | the applicant's city | blank (it is the school's) |
| Zipline (embedded Greenhouse) | Location (City) | "San Jose, Costa Rica" for San Jose, CA | San Jose, California |
| Zipline | available next Spring (Jan to Apr/May 2027)? | Yes, for someone who cannot start before late May | No |
| Kenect (Breezy) | hybrid schedule out of Pleasant Grove, Utah? | Yes, for an Austin applicant who will not move | No |
| JazzHR | gender, race | the page's own pre-selected "Decline to answer" kept | the stated answers |
| Robinhood and others | veteran / military status | "never served" guessed from "not a protected veteran" | only what the answer says (decided with you) |
| Superhuman (Ashby) | education rows | both rows the latest school, the first row's year | each row its own |
| Superhuman (Ashby) | End Date | "October 2017": the page picks today's month when only a year is chosen | blank |
| Superhuman | sexual orientation | "Lesbian" for "Gay or Lesbian" | left for you |
| Brex, Zoox, Vagaro | gender | a decline for a stated "Non-binary" the list lacks | left for you |
| Zoox (Lever) | currently enrolled in a CS program? | Yes, for a graduate | No |
| Zoox | school schedule allowing part-time at the Foster City office? | Yes, for a developer out of school | No |
| Palantir (Lever) | major | "Other" for an MSc in Computing | Computer Science |
| Anthropic, Mindex | in person at our offices / at the Rochester office? | (code) Yes, from the accept-the-requirement default; live they reached the AI | No, for someone who will not move |
| Twitch | permanent resident after your latest citizenship? | (code) "United States" for a yes/no; the page refused it | No |
| Paylocity | School Type | (code) the school's name; no option took it | "College / University" |

### Blanks a stated fact now answers

- **SpaceX:** work authorization offered as five statements ("authorized ... for any employer"), "Citizenship Status" ((b) lawful permanent resident), a major the list lacks ("Other (Technical)").
- **School search lists** that found nothing: "San José State University" (Greenhouse, Ashby), "The University of Texas at Austin" (Greenhouse lists "University of Texas - Austin").
- **Palantir:** the university "last attended", a graduation or high-school year the list does not offer ("Other").
- **Hermeus:** a past internship with its details, a country list for "What is your location?", "If no, will you require sponsorship in the future?".
- **Mindex:** a co-op for a graduate (No), "I currently work here", the Rochester office (No; the page draws its location after the first scan).
- **Zoox:** research and grants for someone out of school (No), a checkbox question that had no label.
- **Paylocity:** per education row, "Did you Graduate?", "Degree Obtained", "Area of Study"; its react-widgets dropdowns now verify (a pick that stuck was reported as failed), and Address Line 1 keeps the street when its autocomplete suggests nothing.
- **Pinpoint:** able to obtain a U.S. clearance (No for a permanent resident).
- **Superhuman:** LinkedIn without "https://" in a URL box, the French degree's field ("Psychologie").
- **Batch B list:** a current MongoDB employee, availability, how soon, "If referred, by who?", 50 states as radios, a lone-Yes consent.

### Decisions (each one rule, easy to reverse)

1. **Education rows are told apart by position** when a form repeats the same
   ids in every row (Ashby). One row on the page is still your main education.
2. **Ashby End Date stays blank for a year-only graduation.** Checked live:
   choosing only the year makes the page set the month to the current one,
   and only the month sets the current year.
3. **Every program finished = not a student**, firmly (it was a "maybe", and
   those questions went to the AI).
4. **A student's questions asked of a graduate:** a co-op, a school schedule,
   research or grants are No; "which degree are you pursuing" and "expected
   graduation" are left blank.
5. **In-person questions that name no full place use the posting's places.**
   An office in your city: Yes. Every office in another state or country, and
   you will not relocate: No. Another city in your own state: yours.
6. **Kept from the AI** (left for you; the AI has the same profile and could
   only guess): government history when a government employer is in it (is
   the Army one?), a clearance level the profile does not name, a high
   school's name, consent to AI notetakers, "If yes, please describe"
   follow-ups, a student's school schedule, a date the profile knows only to
   the month in a day picker, Address Line 2 without a unit.
7. **A U.S. clearance needs U.S. citizenship:** "able to obtain one?" is No
   for a permanent resident or visa holder. A citizen's answer stays yours.
8. **Lists:** a year or school the list does not offer is its own "Other" /
   "not listed" option (native selects only: a search box's loaded options
   are not the whole list). Accents are folded when options are matched.

### Not cases (measured, not testable here)

ADP Workforce Now and iCIMS open their forms only behind a sign-in (no
accounts may be created). A CareerPuck board links out to Greenhouse.
Coinbase opens its form in a new tab, Samsara lazy-loads its iframe only when
scrolled to, Fivetran keeps the form behind an "Application" tab, and both
Rippling postings were taken down.

### Test results

- **Unit:** 1579/1579 (132 files).
- **Round 3, pinned (33 live pages):** 33/33 pages, 541/541 checks on
  `0d08d2f`. The three fixes after it touch none of these pages; Brex-MTL,
  the one candidate, re-ran on `6dfef72`: 20/20.
- **Older pinned suite (86 live pages + 3 framework fixtures):** the first
  run on `0d08d2f` found three regressions (below), all fixed and re-run
  live. Every other difference was read and was an intended round-3 change,
  re-pinned (`8360f68`, `6dfef72`, `079df2b`, `3975b2c`). Final: 88/89. The
  89th is SmartRecruiters' Bosch page, behind its DataDome block on this
  machine, as before this round.
- **Network:** one batch hit DNS failures (`ERR_NAME_NOT_RESOLVED`); its
  pages were re-run and passed. Nothing here is from that batch.

**Harness changes:** a case may let through exact read-only endpoints
(Paylocity's email lookup: blocked, the page broke). The page dump reads a
div combobox's shown value (react-widgets showed as empty before) and numbers
repeated label keys, so a second education row can be pinned. Ashby radios
are pinned by label (their ids change every load), dates by their shape.

**Found by the regression run (fixed, each with a test):**
- Brex's "Do you currently live in, or plan to relocate to…?" stayed blank:
  its options ("Yes, I live here", "Yes, I plan to relocate") have commas, and
  a comma was all it took to read a list as places (`7b6657b` had passed the
  applicant's place to the react-select driver). A place now names a known
  state, province or country, or a name ("Costa Rica"), never a phrase.
- FSSI's "Bachelor's Degree in Computer Science… strongly preferred" got NO
  from the rule meant for Paylocity's "Did you Graduate?", which answered
  outside an education row. It answers in a row only now.
- Paylocity's dropdowns: my first fix read the widget's kept option list as
  its value, so the fill believed a choice was already made. Caught on the
  re-run, fixed before the pins (`0d08d2f`).
- Older than this round: "today" was the UTC day, a day ahead every evening
  in the Americas ("2 weeks from today" at 9 p.m. in Ottawa started a day
  late). It is your own calendar day now.

### Needs you / manual verification

**Decisions you may want to reverse** (each is one rule)
- "Currently enrolled?" is now a firm No once every listed program has
  finished. If a profile lags behind (a new program not added yet), that No
  is wrong until the profile is updated.
- "Able to obtain a U.S. security clearance?" is No for a permanent resident
  or visa holder. U.S. rules grant clearances to citizens (a rare limited
  access for others exists); a citizen's answer is left to them.
- In-person questions get No when every office the posting lists is in
  another state or country and the applicant will not relocate. A remote-
  first company asking about occasional office visits gets the same No.

**Not verified**
- **The AI path.** These runs use a dead AI (the harness's fake API), so what
  the AI writes for the fields still sent to it (essays, "Why X?", the
  questions left to it) was not seen. Fewer fields go to it now: the "kept
  from the AI" list above leaves them for you instead.
- **Paylocity's address autocomplete with a US address.** Verified only that
  a UK street stays when nothing is suggested. If it suggests US addresses,
  the pick path is untested here.
- **Page prefills from the machine's location.** Pinpoint pre-selects
  "Canada" as the address country and Workable (Syntiant) "Gatineau, Canada"
  as the address, from this machine's IP. The extension never overwrites a
  value already in a field, so an applicant abroad keeps the page's guess.
- **Later steps** behind a captcha or the resume wall, as before.
- **Address Line 1 without its unit** when a Line 2 takes it: unit tests
  only; no round-3 page has both for the Montreal persona.

**Known limits**
- Ashby with a year-only graduation leaves End Date blank (month and year to
  fill by hand). Completed degrees store only a year; storing the month
  would let it fill.
- Telemetry still logs Greenhouse's phone country picker as changed ("+1"
  read back for "United States"): no dial-code table.
- Not testable here: ADP and iCIMS (sign-in walls), CareerPuck (link-out),
  Coinbase (new tab), Samsara (lazy iframe), Fivetran (Application tab),
  two taken-down Rippling postings.

---

# Run with your real profile (2026-10-03, evening)

You asked: "run for real with my profile". The extension ran on 40 live
application pages (the complete round's 18 and the fresh round's 22) with your
prod profile. The profile was read with SELECT-only queries and assembled by
the backend's own builder, so the extension saw exactly what prod sends it.
Nothing was submitted: every non-GET request was blocked except GraphQL
queries, so no submissions, uploads or accounts. Your profile copy and the
per-page results stay out of the repo (scratchpad, and the gitignored
`test/e2e/results/`). Same branch, still local only, not pushed, not deployed.

**TL;DR.** The first pass wrote **20 wrong answers (7 causes)** that the test
profiles never triggered, and left **17 questions blank** that your profile
can answer. All are fixed (`93f1d4a`, `5bead3d`, `7c77dac`), each with a test
that fails on the old code. On the fixed build the 20 wrong answers are gone
and the 17 blanks are filled, on the same live pages: 38 in the second full
pass, Robinhood's and Anthropic's in single-page re-runs after their later
fixes (one Lever location dropdown ignored its pick once; a known flaky
widget). What is still blank is missing from your profile (list below) or
genuinely yours to answer: essays, US work authorization and sponsorship,
citizenship, recording consent.

### Wrong answers on the first pass (fixed)

| page(s) | question | wrote | cause | now |
| --- | --- | --- | --- | --- |
| Robinhood, Superhuman | race | one subgroup of a broader answer | the matcher took the first option containing the answer's word, though several options were narrower subgroups of it | left for you: you pick |
| Robinhood | gender identity | a cisgender option | guessed from the gender answer; your gender identity is blank. The list loads only when opened, so the fix needed the retry path too (`5bead3d`) | "I don't wish to answer" |
| ActioNet | current or former government employee? (and 2 follow-ups) | No | the "never a government employee" default ignored your federal internship | left for you |
| ActioNet | Active Clearance/Public Trust? | Yes | your "Active clearance" read as a US clearance | left for you (the AI is kept out too) |
| Lever (Palantir, Hermeus, Zoox, PCC, Neighbor, Arc'teryx, Veeva, Agiloft), Commvault, Rippling (4AG, FluidAI) | Current company | your internship employer | the backend sends the resume's first job whether or not it ended (yours ended 05/2026) | blank |
| Enova | How did you hear? | Campus Career Site | a campus channel was the only "career site" option | LinkedIn (the default order) |
| Anthropic | "the address from which you plan on working? If you would need to relocate, please type \"relocating\"" | your home city | the instruction was ignored; the role is in-office in the US | "relocating" (you said you will relocate) |

### Blanks your profile answers (fixed)

| page(s) | question | now |
| --- | --- | --- |
| 7 of the 8 Lever pages | Current location | "Gatineau, QC, CAN": your bare "Gatineau" matched Gatineau, Quebec and Gatineau, Haiti. On Arc'teryx, Lever ignored the pick once (a known flaky widget) |
| Workable (Mindex, Rave, FSSI) | Title (required) | your internship title, beside the company already filled |
| ActioNet | highest education | Some College |
| SpaceX | GRE | Did not take/Do not recall |
| Voldex | available in Eastern/Pacific hours? | Yes |
| Enova | local to Chicago for summer 2027? | Yes (you are willing to relocate) |
| Netlify | accommodation for the interview? | from the profile's disability answer (No only when it states none) |
| Agiloft | disability (radios with no label) | the option matching the profile's disability answer |
| Planet | how did you first hear about Planet? (required) | Other - Job Site |

### Decisions (new policy; each is one rule, easy to reverse)

1. **An EEO answer broader than the options is left for you**, not declined:
   you stated it, so you pick the subgroup. An exact option still wins.
2. **Gender identity split by cis/trans options needs a stated identity.**
   Without one: an unqualified option that fits ("Man", "Non-binary"), else
   the decline. LGBTQ+ is Yes for a stated orientation or identity that is
   one, No only when both are stated and neither is.
3. **"Current company / title" means a job still running.** The backend sends
   the resume's first job whatever its end date; a field labelled current now
   gets nothing when every job has ended. A bare "Company" / "Title" (an
   experience entry) still gets the most recent job.
4. **A clearance holds in your own country.** A question in another country's
   terms ("Public Trust", "TS/SCI", DOE "Q") or on a job abroad is left for
   you; "None" holds everywhere.
5. **No "never a government employee" default when your history has a
   government employer.** A procurement or government OFFICIAL is a role: a
   developer intern's "No" there stands.
6. **Answers from stated facts that were "blank by design" this morning:** an
   interview accommodation is "No" only when the profile states no disability;
   "will you be local to <city>?" follows your relocation answer (as Brex's
   "plan to relocate" already did).
7. **The default "how did you hear" is never a campus channel**, and among
   several job searches it takes the one no brand names.

### Fix in your profile (these are data, not bugs)

1. **Current title is stored as "No".** The extension now ignores it.
2. **Your salary expectation has a stray space inside the number.** It is
   typed as written (Twitch, Planet, BambooHR, Agiloft), and ActioNet's
   number-only box stays blank because it is not a number.
3. **Earliest start 2026-08-28 is in the past**, so start-date questions get
   today (10/03/2026) or "Immediately". Set it if you are applying for summer
   2027 internships.
4. **No expected graduation.** Your education ends "Present"; the 2028 date is
   only in your resume's achievements text, which no field maps. About ten
   pages asked (ZipRecruiter, Astranis, Figma, Robinhood, Palantir, Hermeus,
   Superhuman, Ramp, Brattle, Enova, 4AG). Fill "Expected graduation".
5. **Work authorization is one general "yes".** US-job authorization and
   sponsorship questions stay blank (correct: nothing says what your US
   status is). Fill "Authorized to work in the US" and "in Canada".
6. **Gender identity is blank**, so cis/trans-split identity questions and
   LGBTQ+ decline. Fill it if you want them answered.
7. **No GPA, languages, cover letter or "how did you hear"**: those questions
   stay blank or take the defaults.

Items 4 and 5 are the new profile fields from today: they persist only once
the backend and web app are deployed together.

### Test results

- **Unit:** 1443/1443 (129 files).
- **Your profile, live (40 pages, nothing submitted):** first pass (build
  `a497700`) 540 writes, 20 of them wrong; second pass (build `93f1d4a`) 538
  writes, 18 of the 20 gone and the 17 blanks filled; the last two (Robinhood
  identity, Anthropic address) fixed and verified page by page on the final
  build.
- **Pinned regression suite** (`results/after-real`, build `5bead3d`): 1457/1490
  checks. Every miss was read: 11 cases differ by the intended answers above
  (re-pinned), one posting was taken down (Workable TSA: API 404, removed
  from the suite), one is SmartRecruiters' DataDome block on this machine (as
  before).
- **Final build, full re-run (`results/final-real`): stopped by the system
  for low memory after 36 of 104 cases, all 36 passing** (9 of the 12
  re-pinned cases among them, Anthropic's "relocating" included). The other
  68 were not re-run on the final build; they passed on `5bead3d` apart from
  the two above, and the final build's later changes (the relocation
  instruction, the dumper's uuid keys) touch none of their questions.
  Re-run with `node build.mjs && node test/e2e/run.mjs` when memory allows.

### Needs you / manual verification

**Your decisions** (both settled later the same evening)
- **Military status:** no longer inferred. "I am not a protected veteran"
  fills only an option saying exactly that; the profile gained "I have never
  served in the military", which fills every never-served wording (`fbbe0bf`,
  see Round 3 above).
- **Planet's "How did you find this position?"** keeps "Other - Job Site", as
  you confirmed: job sites come first in the default order (a posting found
  through Tailrd was found on a job site).

**Not verified**
- **Resume attach:** not exercised (uploads are blocked in these runs). Your
  stored PDF exists in prod (`has_file` true), so auto-attach has a file.
- **Later steps:** 17 pages stop at the captcha and 12 at "attach your resume"
  after step 1, so a second page of questions (if any) was not seen.
- **The follow-up dialog** that asks you about what stays blank (the race
  subgroup, for one) was not opened in these runs.
- **Flaky widgets, both seen with test profiles too:** Lever's location
  (Arc'teryx ignored the pick once) and Rippling FluidAI's location ("couldn't
  open the dropdown").

**Backend follow-up (not changed tonight)**
- `backend/routers/profile.py:431` sends the resume's first job as
  `currentCompany` whatever its end date, and `currentTitle` falls back to
  that job's title only when the stored job title is empty (yours holds "No",
  so the fallback never runs). The extension compensates now; anything else
  reading the profile still sees an ended internship as current.
- The stored title "No" most likely came from the extension's old follow-up
  dialog saving a yes/no into the title slot; that save is blocked since
  `3c0c919`. Onboarding no longer writes the job function there (`a497700`).

---

# Day session: answer every question without AI (2026-10-03)

Same branch, still local only (NOT pushed, NOT deployed). You asked for three
things: Brex's form filled properly; every question answered without AI
(mapping and defaults, with AI only for essays and the genuinely unanswerable);
and new profile fields wherever no mapping could supply an answer, added to the
extension panel, the web app and onboarding. Then: test on real pages, fix,
retest. The overnight notes below are unchanged except where this section
says it supersedes them.

**TL;DR.** The extension now answers the standard screening questions on the
device: consent, posting requirements, prior applications and referrals,
relatives inside, conflicts, government officials, how you heard about the
job, relocation, EEO (declined when the profile says nothing), start dates, test
scores, export-control status and more. AI is left for essays and judgement
questions. Five profile answers no mapping can supply (US / Canada work
authorization, how you heard, expected graduation month, GPA) are in the
extension panel, the web Profile page, onboarding and the backend. Brex now
fills completely: every question is answered except three correctly blank
follow-ups.

Testing on real pages found real bugs in every round:
- **Complete-profile round:** "no sponsorship needed" for US jobs on 7 pages,
  caused by that morning's own new per-country field. Also a mangled BambooHR
  date, an invented graduation day, an H-1B history question answered as
  sponsorship, and a dropdown counted as filled when it was empty.
- **Blind round on 22 postings never seen before:** 9 wrong writes.
- **Regression runs of the pinned suite:** 3 more (ActioNet, ZipRecruiter, an
  Ashby end date).
- **Read-through of my own new rules:** 3 that would have over-reached, plus one
  answer that was right only by luck.

All are fixed, each with a test that fails on the old code. On the final build:
- the pinned regression suite passes **64/64 cases (42 live pages), 825/825
  checks**;
- the two new committed live rounds (40 more pages, 626 reviewed pins) pass
  **40/40**, the run they were pinned from;
- unit tests: **1411/1411**.

Not pushed, not deployed.

### What changed today (commits on top of the overnight `c0b43f3`)

| commit | what |
| --- | --- |
| `ce65496` | **Default answers on device** (`defaultAnswers.ts`), **EEO decline**, **consent policy**, react-select options re-resolved on device, a recovery pass for fields the page clears, a pass for questions that appear during the fill |
| `1c63c18` | e2e expectations re-pinned to the new policy (101 new writes reviewed one by one first) |
| `15ddffc` | **Five new profile answers** across the extension panel, web Profile page, onboarding and backend |
| `bd01bfe` | **The job's city**, and an embedded form asks the page around it where the job is |
| `7f217c1` | **Conditional questions** answered only when their condition holds; "what sponsorship would you require?" no longer gets the authorization statement |
| `a11f4a3` | `COMPLETE_CANADIAN` e2e profile (every screening answer filled in) |
| `22f7628` | Never "no sponsorship needed" for a country the applicant cannot work in; citizenship kept beside the explicit answers |
| `3702db9` | Month-name date pickers ("dd mon yyyy"); a graduation month never becomes a day |
| `a167849` | Education end month from the expected graduation; Ashby's unlabeled date selects; "Still Student?" |
| `d4505e3` | The blanks a complete profile left on live pages (work right, employment history, export status, test scores, channels, acknowledgements, SMS, gender identity, …) |
| `93d19b5` | Languages checklist, "Other" for an unlisted major, the disability form's signature date |
| `7f65ee4` | An open list's highlighted option is not a selection |
| `b7e71e1` | H-1B history; Ashby graduation radios; "Still Student?" selection; label-only fill diagnostics; e2e `afterFill` hook |
| `1ee0260` | Blind round on 22 fresh postings: 9 wrong writes and the blanks behind them (incl. same-job-only carry-over, "Country - City", form-implied job country) |
| `b455627` | A current student's Ashby End Date is the page's; "did you graduate from" is no date |
| `93966d7` | Three default rules narrowed before they meet a page |
| `4004008` | Start-time spans; no dates in numeric buckets; demographic synonyms at scan time; form-country hint retried |
| `926f39a` | A stated channel offered several ways picks its job-search variant |
| `23e1f67`, `441016a` | The two live rounds committed as cases (`real-live-complete.mjs`, `real-live-fresh.mjs`); re-pins after review |

### Decisions (these supersede overnight decisions #1 and #7 where they differ)

1. **Defaults answer as a typical applicant who accepts the posting's terms
   and is unencumbered.** Yes to: application consents and certifications,
   in-office / on-site / hybrid / travel / schedule requirements, background
   checks, essential functions, being considered for future roles. No to: applied
   or interviewed before, referred by an employee, relatives inside, conflicts of
   interest, non-competes, government official, background-check obstacles,
   marketing / SMS / newsletter opt-ins. A profile fact always wins over a
   default (a stated "won't relocate" answers No).
2. **Never defaulted:** work authorization, sponsorship, citizenship, criminal
   history, relocation ASSISTANCE (a request, not a requirement), recording /
   AI-notetaker consent, and in-office requirements when the profile says Remote.
   Those stay blank unless the profile states them.
3. **Consent: every application consent is given, including the
   demographic-data consent.** You asked for all questions to be answered, so
   this reverses the overnight fix for bug #36 by design. Decision #7's
   inconsistency is gone: clear consent wording is always ticked.
4. **EEO with no profile answer → the decline option** ("Decline to
   self-identify", "I don't wish to answer"). It discloses nothing and stops a
   required EEO question from blocking submit. A list with no decline option
   stays blank (Workday's veteran select).
5. **"How did you hear about us"**: the profile's answer, mapped onto the form's
   wording ("Career fair" → "University Career Fair"). With none set: a job-board
   option, else the company website, else "internet", else LinkedIn, else
   "Other". Free text gets "Online job board".
6. **"Are you located here, or would you relocate?"**: "located here" when the
   profile city is the job's city, "relocate" otherwise, "No" when the profile
   says it won't relocate.
7. **A question with a condition on the applicant** ("If you are a current or
   former government employee, …?") is answered only when the condition holds.
   False → the option saying it does not apply, or blank. Unknown → blank.
   Hypotheticals ("If you are offered the position, …") hold.
8. **A required list whose ONLY option is an acknowledgement is answered with
   it.** This includes Anthropic's "Agreement to Arbitrate" ("I understand and
   agree to the terms…"), a waiver of the right to sue. That follows from "answer
   every question" and the form offers nothing else, but it is a legal agreement
   made on the user's behalf, so **you may want it excluded** (one regex in
   `defaultAnswers.ts`, `ACK_OPTION`).
9. **No test scores on the profile → "Did not take / Do not recall"** when the
   list offers it (SpaceX's SAT/ACT/GRE). It claims nothing. A free-text score
   box stays blank.
10. **U.S. export-control status is inferred only from US work authorization.**
    Not authorized in the US means none of citizen / permanent resident / refugee
    / asylee / DACA (each of those may work there), so "Other" / "None of the
    above" / "Foreign person". Nothing said about the US → blank.
11. **"Open to relocation?" answered with a list of cities → the job's own
    city** when the applicant is willing to relocate; "No" (or the remote
    alternative for a Remote preference) when not.
12. **Backend scope widened.** The new fields needed `backend/routers/profile.py`
   and `fill.py` (no migration: they live in `user_settings.prefilled_answers`
   under exact keys, never mined from other answers). The overnight run was
   extension-only; today's request required the web app and backend too.

### New profile answers (no mapping can supply these)

`authorizedUS`, `authorizedCanada` (Yes/No), `howDidYouHear`,
`expectedGraduation` (a month, `YYYY-MM`) and `gpa`. They are in the extension
panel (Preferences), the web Profile page, the backend profile API and the AI
fill context. Onboarding asks the two authorization questions (Role step) and the
expected graduation month (Experience step), and saves only what was answered (a
blank stays "not answered", never "No"). The option lists are twins pinned by
tests on both sides (`SCREENING_CHOICES` ⇄ `SCREENING_OPTIONS`).
What they unlock: "authorized to work in the US?" for a Canadian (before:
always blank), "When do you graduate?" with month-range options ("January - June
2027", "Spring 2027", "December 2026 - November 2027"), GPA buckets, and the
user's real channel instead of the default.

### Bugs found on real pages today, each fixed with a regression test

**Wrong answers written (the serious ones):**
1. **"No sponsorship needed" for a US job from a Canadian not authorized in the
   US** (`22f7628`). The general "requires sponsorship: No" (meant for Canada)
   beat the explicit "authorized in the US: No" for every US question: Hermeus
   got "No, I do not require sponsorship" for "…employment in the USA?", and
   Robinhood, ZipRecruiter, Twitch, Mindex, Palantir and Anthropic got "No". This
   bug came in with the new per-country field (`15ddffc`) and was caught by the
   complete-profile round before it shipped anywhere. Now: not authorized there →
   sponsorship needed there; an unscoped question with the job's country unknown
   stays blank for such an applicant; "require sponsorship … to legally work in
   the U.S." is a sponsorship question, not an authorization one.
2. **"If you are a current or former government employee, have you recused
   yourself…?" answered "No"** (ActioNet, `7f217c1`), which reads as an unrecused
   conflict. The form offered "I am not a current or former government
   employee". Conditional questions now evaluate their condition first.
3. **"…what sponsorship would you require?" got "Canadian citizen"** (Brex,
   `7f217c1`): the type of sponsorship is the applicant's to say; now blank.
4. **"Date Available" typed as "05 mon yyyy"** (BambooHR, `3702db9`): its Fabric
   date picker wants "03 May 2027" and mangled our "05/03/2027".
5. **A graduation month became a day** (Ramp's Ashby picker got "04/01/2027",
   `3702db9`). The day is unknown, so a day-precise picker stays blank.
6. **"Cisgender man" could be picked for a cisgender woman** listed after it
   (`d4505e3`); identity and gender must now both match.
7. **"Relocation assistance?" answered Yes**, **"currently enrolled OR graduated"
   answered No for graduates**, and **"a university" read as a school name**
   (`ce65496`, `7f217c1`).
8. **A dropdown counted as filled when it was not** (`7f65ee4`). An open list's
   highlighted option (`aria-activedescendant`) was read as the selection, so
   SpaceX's "Employment History" was skipped as "already showing the answer",
   reported as written, and confirmed by the end-of-fill check. The page held
   nothing. This could hit any react-select left open with the answer
   highlighted.
9. **"Have you held H-1B status…?" answered as a sponsorship question** (Yes,
   Twitch; `b7e71e1`).
10. **Blind round on 22 postings never seen before** (`1ee0260`), complete
    profile, 9 wrong writes:
    - "Preferred first and last name" → last name only.
    - "enrolled in a PhD program?" → Yes for a bachelor's student.
    - "any impediments to traveling internationally?" → Yes.
    - "If you have under 2 years…" → "N/A - I have more than 2 years" (she has 1.4).
    - "1–2 years of experience" → NO.
    - "school, program and expected month/year" → "2027".
    - Arc'teryx sponsorship → Yes for a job in Canada. The job's country was
      carried over from the previous Lever posting: every Lever company shares
      one host. Carry-over now requires the same job's URL path.
    - Rippling "Current company" → the first job (sequential ids "field-31"
      read as row 31).
    - A US-citizenship requirement that resolved to "Canada". It was blank only
      because no option matched.

    All 9 are fixed with tests that fail on the old code.
11. **Regression runs of the pinned suite** found three more:
    - ZipRecruiter: "What school are you currently attending / did you graduate
      from?" → school plus graduation date. The verb "graduate" is not a date
      question.
    - Ashby (Ramp, Superhuman): a disabled End Date read "October 2027". Ticking
      "Still Student?" disables End Date, and a year written first was left
      beside a month the page picked. A current student's End Date is now left
      to the page (`b455627`).
12. **Read-through of my own new rules** (`93966d7`, `4004008`):
    - A lone "Yes" option would have been taken as an acknowledgement ("Have you
      applied before? [Yes]").
    - "Professional Certification [Yes/No]" would have been read as consent.
    - "travel with no restrictions" would have been an obstacle question.
    - Striveworks' "12+ weeks from offer acceptance" was right only by luck: the
      option matcher read the start date's year (2027) as a number of weeks. A
      start next week would have landed there too.

**Blank answers the profile could give (fixed):** Brex's "authorized to work in
the stated location" (the job's place is now read from the page around an
embedded form, `bd01bfe`); Greenhouse/Ashby education end month, "Still
Student?" and Ashby's unlabeled year selects (`a167849`); citizenship country
after the new per-country answers (`22f7628`); relocation answered with a list
of cities (the job's city), "a Twitch employee", "legally eligible to begin
employment", employment history asked through its options, US export-control
status, graduate GPA "Other/Not Applicable", SAT "Did not take/Do not recall",
"how you heard" / "how did you connect" / an unlabeled channel list, single-option
acknowledgements (Anthropic's arbitration agreement), Anthropic's "AI Policy"
Yes, SMS consent asked through options, "No" typed into "Were you referred? If
so, who?", "join as an intern" start date (`d4505e3`); languages checklist,
"Other" for an unlisted major, the disability form's signature date (`93d19b5`);
a current student's "Still Student?" and Superhuman's graduation radios
(`b7e71e1`); "in the country that you are located", "at least 18 years or
older", "previously worked for this organization", "how did you FIRST hear",
preferred start dates offered as dates, "living in the US or Canada?", the time
zone, SMS "via text", "I understand…" acknowledgement boxes, future-job opt-ins,
and the job's country implied by the form's own work-auth questions (`1ee0260`);
start-time spans ("Over a month from offer"), Ashby's gender boxes ("Woman"),
and a stated channel offered several ways (`4004008`, `926f39a`).

**Not a gap:** Workable's `#city` / `#postcode` / `#country` are `aria-hidden`,
`tabindex=-1` helper inputs, not questions. Filling hidden inputs is what
blocked Workday's submit before (overnight bug list), so they stay empty.

### Test results

- **Extension unit suite:** 1411/1411 in 129 files (1331 in 128 at the start of
  the day). Every bug above has a test that fails on the code before its fix. I
  checked each one by swapping in the old file and running it.
- **Web app:** profile parity + settings 19/19, onboarding (SetupWizard) 13/13.
  The new onboarding and Profile fields rendered in a real browser at 13
  viewports (responsive audit: 0 high, 0 medium findings).
- **Backend:** profile + fill-profile tests 40 passed (run isolated, see
  overnight notes).
- **Real-extension e2e, final build (`results/final-1`):**
  - **Regression suite: 64/64 cases (42 live pages), 825/825 checks**, scored at
    run time against pins set before the run.
  - **Two new committed live rounds:** `real-live-complete.mjs` (18 pages,
    complete profile) and `real-live-fresh.mjs` (22 new postings). 40/40 against
    pins taken from that same run, so that number is a baseline, not a test. The
    pins were checked against the earlier reviewed run (`results/all-2`), where
    every write was read by hand. The final run differed from it only by the
    reviewed improvements and one intermittent blank.
  - The earlier reviewed run, re-scored against the final regression pins:
    63/64. The miss: Twitch's Degree dropdown left blank once (Greenhouse's
    async dropdowns drop occasionally; blank, never wrong).
- **Extension panel and web Profile page:** screenshots show the five new
  answers in the panel's "Your Autofill Information → Preference" (filled from
  the profile) and as rows on the web Profile page (responsive audit: 0 high, 0
  medium).

### Needs you / needs manual verification (today)

**Your decisions**
1. **Anthropic's arbitration agreement** is now accepted on the user's behalf:
   a required list whose only option is "I understand and agree…" (decision 8
   above). Say if you want legal waivers excluded.
2. **"I have never held H-1B status" is a default** for profiles that name no
   US visa; "Did not take / Do not recall" for SAT/ACT/GRE; "Other" for an
   unlisted major. Each is the answer that claims nothing, and each is easy to
   reverse.
3. **Rippling picks its phone country from the browser locale.** This test
   machine's Chromium runs en-GB, so it shows "+44 GB" (seen with no extension
   loaded). A user on en-US or en-CA gets "+1", so this is not a real-user
   problem, and the extension never overwrites a pre-filled value (overnight
   decision 8). Tell me if your users' locales vary enough to need a
   dial-code exception.

**Needs manual verification**
- **Deploy together.** The new profile answers are saved through the backend's
  profile API (`profile.py`, `fill.py`). Until that backend is deployed, prod
  ignores the unknown keys, so answers entered in the panel, the Profile page
  or onboarding will not persist. Ship backend and web app in the same push.
- **Web onboarding and Profile page:** rendered in a real browser at phone and
  laptop sizes (responsive audit: 0 high, 0 medium; the radios and the month
  input work), and covered by unit tests. A real save against the backend was
  not run (no deployed backend with the new keys).
- **Your installed extension is the old build** (prod report 183 from Brex had
  an empty extension version). `cd chrome-extension && node build.mjs`, then
  reload the unpacked extension.
- **Intermittent, not fixed:** Hermeus's Lever location typeahead stayed blank
  once (filled in the previous run); Greenhouse's async School dropdown
  (Robinhood) as before. (SpaceX's GRE was listed here as a dropdown that
  opened too late. Wrong: it was blank on every run, because its two "did not
  take" options tied. Fixed in the real-profile run, see the top section.)
- **Still blank by design:** essays, opinions, skill-specific questions
  ("experience with AI?", years of Roblox Studio), the applicant's
  extracurriculars, interview-recording consent, sponsorship TYPE.
  (Accommodation requests and "local to Chicago for summer 2027?" were on this
  list; the real-profile run answers them from stated facts now, see the top
  section.)

---

# Overnight run: deterministic autofill (2026-10-03)

Branch: `night/deterministic-autofill` (local only, NOT pushed, NOT deployed).
Scope: `chrome-extension/` only. AI is out of credits, so every number below is
measured with the backend's AI pass returning nothing.

> Status: DONE for the night. Everything below was run and observed unless it
> is listed under "Needs manual verification" (section 5).

**TL;DR.** With the AI returning nothing, the extension now answers from the
profile alone and leaves the rest blank. On 64 real-extension cases (42 of
them live application pages) it passes **774/774 checks with zero wrong
writes** (four of those checks accept a blank; see section 4). Twenty-four of
the live pages were drawn fresh after the main fixes and reviewed blind: the
first twelve showed 14 wrong values on 6 pages, the next twelve (after fixing
those) 1 wrong write on 1 page. All fixed and pinned, but expect a few more on
pages nobody has looked at.
The `main` build, re-scored on the 40 of those cases that predate the
held-out round, scored 387/478 and wrote 13 invented answers ("authorized to
work in the US? Yes" for a Canadian citizen, among others) plus 14 wrong
values; the branch passes all 40. Nothing was pushed or deployed. Section 5
lists what needs you.

**How to run it yourself** (from `chrome-extension/`):
- Unit suite: `node node_modules/vitest/vitest.mjs run` (`npm test` exits 1 with no
  output in some shells; run vitest directly).
- Real-extension e2e: `node build.mjs && node test/e2e/run.mjs` (all 66 cases,
  about 20 min, opens a Chromium window; the two live SmartRecruiters cases
  time out while DataDome blocks this machine). `--filter live` / `framework` /
  `synthetic` / `inputs` / an ATS name / a case id narrows it; `--report-only` exits 0 for
  measurement runs. `E2E_DEBUG=1` prints the extension's console per case.
- Older harness and probes: `npm run test:browser`, `npm run test:flow`,
  `npm run test:workday-account`, `npm run test:workday-gate`,
  `npm run test:workday-churn`, `npm run test:driver`, `npm run test:click-shadow`,
  `npm run test:extension`.

## 1. Testing infrastructure

**What worked (no fallback needed):** real Chromium + the real extension.
Playwright 1.61.1 and Chromium 1228 were already installed. This is Windows, so
there is no xvfb: Chromium runs HEADFUL natively (a window opens), with
`--load-extension=dist --disable-extensions-except=dist`. Nothing failed to
install. (`HEADLESS=1` switches to `--headless=new`, which also loads
extensions.)

New harness, `chrome-extension/test/e2e/`:

| File | What it does |
|---|---|
| `fakeApi.mjs` | Local stand-in for the Tailrd backend. The extension is pointed at it through its own settings, so the shipped bundle is untouched. Serves the profile sync, answers `/api/fill` the way a credit-less AI does (no answers, `ai_error`), turns diagnostic capture ON and records every telemetry report. |
| `harness.mjs` | Launches Chromium with `dist/`, seeds config/auth, serves pages at their REAL ATS URLs by route interception (so site adapters match), presses the panel's own Autofill button, follows the flow's progress beats until it parks, then dumps the page. |
| `dumpFields.mjs` | Reads EVERY field's final value and type from the page DOM (open shadow roots, every frame). This is the ground truth, independent of what the extension believes it wrote. |
| `evaluate.mjs` | Scores a case: expected values per field, PLUS every field that changed without an expectation is an UNEXPECTED write (the wrong-kind / invented-answer check). Framework pages also assert the framework's own model state. |
| `run.mjs` | `node build.mjs && node test/e2e/run.mjs [--filter x] [--report-only]`: per-case and per-ATS report, JSON in `test/e2e/results/` (gitignored). |
| `capture.mjs` | Captured the real application pages (below) from jobs in the app. |

Safety: in live mode only reads leave the browser. GET/HEAD/OPTIONS go out,
plus GraphQL POSTs whose operation is a `query` (Ashby renders from those).
Every other request is aborted (submits, uploads, mutations, autosave,
analytics beacons). No form was submitted, nothing uploaded, no account created.

Test corpora:
- **Live real pages** (`cases/real-live.mjs`): application forms from active jobs
  in prod `scraped_jobs` (Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
  BambooHR, Jobvite), with a realistic sparse profile (one location string,
  "Canadian citizen", co-op in progress). BambooHR is also run the way a user
  does it: open the form, press Autofill a second later
  (`live-bamboo-nexthop-user-opened`). One Greenhouse form is embedded the way
  career sites do it: an employer page with a cross-origin `grnhse_iframe`
  (`cases/real-live-embedded.mjs`).
- **Same live pages, other applicants** (`cases/real-live-profiles.mjs`): a US
  F-1 student with a structured Cambridge, MA address (Workable, Jobvite), and a
  profile whose whole address is one street line (BambooHR).
- **Held-out live pages** (`cases/real-live-heldout.mjs`): two rounds of 12
  postings drawn at random from prod `scraped_jobs` AFTER the fixes above,
  from companies not in the corpus (round 1: Ashby 4, Greenhouse 4, Lever 3,
  Workable 1; round 2: Ashby 4, Greenhouse 4, Lever 4). Each first pass was
  blind: no expectations, every write reviewed by hand. Round 1 found bugs
  #30-#35; round 2 (after those were fixed) found one, #36. All pinned.
- **Inputs** (`cases/inputs.mjs`): date inputs (native `date`, split M/D/Y,
  masked text) and a real résumé file upload, checked in the page.
- **Captured real pages** (`test/fixtures/real/`): rendered DOM of those pages, for
  the jsdom regression suite (`test/realPages.test.ts`), 1.9 MB after trimming CSS.
- **Framework pages** (`cases/frameworks.mjs`): React 18 controlled inputs (incl. a
  phone mask and a re-render on every blur), Vue 3 `v-model` (`.lazy`, `.trim`,
  `.number`, name-less radios), AngularJS 1.8 `ng-model` (`updateOn: 'blur'`,
  `ng-options`). Asserts the value registered in the FRAMEWORK STATE, not just the
  DOM. Libraries vendored in `test/e2e/vendor/` (MIT).
- **Synthetic ATS fixtures** (`cases/synthetic.mjs`): the repo's 16 jsdom fixture
  builders, bundled and served on each ATS's real host. This is the only coverage
  for Workday, iCIMS, Taleo and SuccessFactors (see section 5).

The pre-existing in-page harness (`npm run test:browser`) still runs; it had 8 of
19 scenarios failing on clean `main` (stale expectations, see section 2).

## 2. Bugs found and fixed (each with its regression test)

Every item was reproduced first (test failing, or a wrong value on a live page),
then fixed. "Live" = observed with the real extension on a real ATS page tonight.

| # | Bug | Where seen | Regression test |
|---|---|---|---|
| 1 | **Work authorization was country-blind**: "Authorized to work in Canada" (or "Canadian citizen") answered "Are you legally authorized to work in the **United States**?" **Yes**. Same for sponsorship. | Live: Greenhouse ×2, Workable | `questionResolver.test.ts` "work authorization, country-aware"; `realPages.test.ts` Greenhouse/Ashby/Workable; `workday.test.ts` "this country" |
| 2 | Abstained legal questions still went to the backend, whose rule pass answers "authorized to work?" Yes and "sponsorship?" No **unconditionally**. They now never leave the device. | Code read (`backend/routers/fill.py` `_raw_rule_based_answer`) | `realPages.test.ts` (`deviceAbstained`) |
| 3 | **Ashby radio questions took the PREVIOUS question's label** (`<fieldset><label for=…>` with no `<legend>`): "Do you think AI will take over the world?" was read as work authorization and answered **Yes**. | Live: Ashby | `realPages.test.ts` Ashby |
| 4 | **Lever radio questions had no label** (question in a sibling `.application-label`): labelled with the input's `name`, never recognized. | Live: Lever | `realPages.test.ts` Lever |
| 5 | City got the whole "Toronto, ON, Canada"; a bare "Address" next to City/Postal got the location string. | Live: BambooHR, Jobvite | `addressFields.test.ts`, `realPages.test.ts` BambooHR/Jobvite |
| 6 | "Do you anticipate challenges clearing a background check?" got the **degree title**; "Where did you complete your undergraduate degree?" got the degree title instead of the school; "If 'Other' selected for School Name…" got the school. | Live: Lever, Greenhouse | `realPages.test.ts`, `questionResolver.test.ts` |
| 7 | "Let the company know about your interest working there" (textarea) got the **employer's name**. | Live: SmartRecruiters | `realPages.test.ts` SmartRecruiters |
| 8 | A Lever commute question ("…the Toronto office located at… Are you able to…") was answered as a residence question (Yes). | Live: Lever | `questionResolver.test.ts` "applicant must be the subject" |
| 9 | Option matching ignored negation: "I am not a protected veteran" tied 100% with "I am a protected veteran" and option ORDER decided. Also: "no" matched inside "not", ties went to the first option, a number on a bucket boundary went to the first bucket, "Over 6 years" was unparsed, a place name could land on a Yes/No option through a shared word. | Old harness tie; unit repro | `optionMatchStrict.test.ts` (14) |
| 10 | **The page observer watched the panel's own shadow root**: every panel repaint caused a rescan, which repainted the panel. A rescan every 500 ms on every page while the panel was open. | Live logs (hundreds of repaints per page) | `observePage.test.ts` |
| 11 | **Lever's location typeahead never stuck** (it clears an entry not picked from its list). Now types the city and clicks Lever's own suggestion, matched as a PLACE ("Toronto, ON, CAN"). | Live: Lever ×3 | `leverAdapter.test.ts` (live suggestion list verbatim) |
| 12 | **Comboboxes inside web components**: IDREFs were resolved with `document.getElementById`, which cannot see into shadow roots. SmartRecruiters' City menu lives in the parent component's shadow root. | Live: SmartRecruiters | `comboboxShadowControls.test.ts` |
| 13 | **Radios with no `name`** (Vue `v-model`, hand-rolled React) were one-option groups that no answer could select. | Vue 3 page | `namelessRadios.test.ts` |
| 14 | A `groupIndex` parsed from an id ("…-labeled-radio-0") disabled question shapes outside employment/education rows. | Ashby jsdom | `realPages.test.ts` Ashby |
| 15 | Experience-row "Start Date" fields were briefly read as availability questions during the work (caught by the existing suite before commit, fixed: availability needs explicit wording). | `experienceFields.test.ts`, `greenhouseLyftScan.test.ts` | existing tests |
| 16 | **Options past the 60th were dropped as "not offered"**: the scan-time gate checked proposals against the panel's 60-option copy. Every native country select with a full list dropped "United States" (position ~235); Lever's picker dropped "University of Waterloo". Pre-existing. | Live: Lever | `longSelects.test.ts` |
| 17 | **select2 / chosen selects**: the hidden native `<select>` was skipped (hidden + aria-hidden) and the styled proxy fought. Now the native select is filled, labelled by its question, and the proxy is ignored. | Live: Lever | `realPages.test.ts` "select2" |
| 18 | "Are you **able to work** from our Kepler office as required?" was answered as a work-authorization question (from citizenship). | Live: Lever | `questionResolver.test.ts` "'able to work'" |
| 19 | A phone "Country code" picker classified as the applicant's location. | Live: SmartRecruiters | `fieldMatcher.test.ts` "phone country code" |
| 20 | **Comboboxes built from web components**: options slotted into a shadow-DOM listbox were not found; option and selected-value text projected through `<slot>` read as empty; IDREFs into a sibling component's shadow root unresolved. SmartRecruiters' City and Country code never filled. | Live: SmartRecruiters | `comboboxShadowControls.test.ts` (5) |
| 21 | **Place suggestions matched by tokens**: "Toronto, OH, US" scored as a PERFECT match for "Toronto, ON, Canada" (short tokens dropped). City/location dropdowns now pick by place (city, region by code or name, country by name/ISO-2/ISO-3); ambiguous lists are refused. | Live: SmartRecruiters, Lever | `comboboxShadowControls.test.ts` placeHint, `leverAdapter.test.ts` |
| 22 | Material UI wraps native radios in `role="radiogroup"` elements; each was scanned as an empty ARIA group and logged a failed fill next to the native group that filled. | Live: BambooHR | `realPages.test.ts` "wrappers" |
| 23 | **Backend answers skipped the kind gate**: the backend's rule pass answers "city" for any label mentioning a location, which could put "Toronto" into a yes/no text question. Backend answers now pass the same gate. | Code + unit repro | `aiFillPlanner.test.ts` "must fit the field's kind" |
| 24 | Field of study "Applied Science in Mechatronics Engineering" (prefix list misses "of Applied Science"); "Bachelor's degree in progress" gave "progress" (pre-existing). | Live: Workable | `workdayFieldOfStudy.test.ts` |
| 25 | Workday application steps show no job location, so "authorized to work in this country?" could never be answered there. The job country now comes from Workday's URL slug (`/job/Cambridge-MA/…`) or is carried from the posting page. | Code read | `jobLocation.test.ts` |
| 26 | "Will you require relocation assistance or visa sponsorship?" would have been answered from the sponsorship half alone; "120k" salary into a number field gave 120. | Unit repro | `questionResolver.test.ts`, `fieldResolverKinds.test.ts` |
| 27 | **Rows the fill pass adds itself were never filled**: the pass clicks "Add education / experience" for the profile's entries, but only the ids picked at click time were written. | Live: Workable | `fillSelection.test.ts` |
| 28 | **A form the site still holds hidden was scanned as empty.** BambooHR keeps its form mounted inside a `display:none` box for 2.5-5 s after "Apply for This Job" (measured with a page-side probe). No node is added at the reveal, so the DOM-quiet wait passed at once, every control looked hidden, and the flow parked on an empty page: live-bamboo-nexthop failed about half of combined runs. Now waits (bounded, 10 s) while most of the page's typeable controls are mounted-but-hidden, and rescans as they render. | Live: BambooHR | `hiddenForm.test.ts` (7), `flowController.test.ts` "apply entry", live `live-bamboo-*`: 24/24 over 6 runs, then 20/20 over 5 on the final build |
| 29 | **Autofill silently did nothing** when no field was picked and no "Apply" button was visible, although the button is deliberately always live. Hit by anyone who opens a BambooHR form and presses Autofill within those 5 s. | Live: BambooHR | `overlayAutofillClick.test.ts` (fails on the old code), live `live-bamboo-nexthop-user-opened` |
| 30 | **A date picker re-parsed a partial date into a wrong day.** "What is your graduation date?" is a react-datepicker; the profile's graduation YEAR "2027" was typed in and the picker showed **12/31/2026** (`new Date("2027")` is UTC midnight, the evening before in Toronto). Date controls (native type, a format placeholder, or a known picker library) now take only a whole date, re-emitted in the control's format; otherwise blank. Backend answers too. | Held-out live: Ashby (Ramp) | `dateControl.test.ts` (7), `fieldResolverKinds.test.ts` date controls, `aiFillPlanner.test.ts` |
| 31 | **A bare year inside several options was a coin flip**: "2027" picked "December 2027" over "January - June 2027" for having fewer words. A bare number in several options is refused. | Held-out live: Ashby (Superhuman) | `optionMatchStrict.test.ts` "bare year" |
| 32 | **Greenhouse education rows got the first JOB's dates**: the education block has its own start/end month and year (`.education--date-container`), which classified as employment dates. End date = graduation (a bare year fills only the year); start date (unknown) stays blank. Older Greenhouse markup wraps EMPLOYMENT in `.education-experience-block`, so a wrapper naming both is ignored. | Held-out live: Greenhouse (Twitch, Astranis) | `educationRowDates.test.ts` (4; 3 fail without the fix) |
| 33 | **"Mechanical Engineering" for a Mechatronics student**: the matcher counted any shared 5-letter stem as the same word. A variant now differs only by an ending of at most 3 letters (Canada/Canadian/Canadien still match). | Held-out live: Greenhouse (Astranis) | `optionMatchStrict.test.ts` "sharing a stem" |
| 34 | **"High School Name" got "University of Waterloo"**, and "Year of High School Graduation" the university's 2027. High-school detail questions abstain (a yes/no about a diploma is left to the education-level rules). | Held-out live: Lever (Palantir) | `questionResolver.test.ts`, `fieldResolverKinds.test.ts` high school |
| 35 | **The react-select / Workday driver had its own looser matcher**: substring containment, first containing option wins. A graduation year committed "December 2026 - November 2027" though "December 2027 - November 2028" fits too, and "male" sits inside "female". `matchOption` moved to `optionMatch.ts` (pure) and the page-world driver now uses it. | Held-out live: Greenhouse (ZipRecruiter) | `mainWorldDriver.test.ts` (fails on the old matcher) |
| 36 | **A demographic-data consent checkbox was ticked for the user**: "By checking this box, I consent to Robinhood collecting, storing, and processing my responses to the demographic data surveys above", for an applicant who answered none of them. It classifies as a demographic field at high confidence, so the consent "yes" passed the selection gate. A sensitive box now carries only the user's own answer. | Held-out round 2 live: Greenhouse (Robinhood) | `checkboxIntent.test.ts`, `fieldResolverKinds.test.ts` (fails without the fix) |

Old in-page harness (`test/browser/run.mjs`): its résumé-upload scenario failed only
because the harness did not `await` the now-async `injectResumeFile` (harness bug,
fixed). With the product fixes above it is **19/19** (was 11/19 on clean `main`).
The other real-browser probes (`flow-probe`, the three Workday account/churn
probes, `react-select-driver`, `click-shadow-probe`, `load-extension`) all pass.

New test files: `optionMatchStrict`, `profileFacts`, `questionResolver`,
`realPages` (32 assertions on captured real pages), `namelessRadios`,
`longSelects`, `comboboxShadowControls`, `jobLocation`, `fieldResolverKinds`,
`fillSelection`, `hiddenForm`, `overlayAutofillClick`, `dateControl`,
`educationRowDates`, plus additions to `leverAdapter`, `observePage`,
`fieldMatcher`, `aiFillPlanner`, `workdayFieldOfStudy`, `workday`,
`flowController`, `mainWorldDriver`. Unit suite: **1303 passing in 127 files**
(was 1101).

Harness bugs found and fixed along the way (not product bugs): clicking Autofill
before an SPA form rendered, waiting out the full timeout when a fill errored,
treating the end of a zero-field entry page as the end of the flow, an ATS draft
(Workable restores the previous applicant's answers from site storage) leaking
into the next case (storage and cookies are now cleared per case), and the
harness clicking BambooHR's "Apply" on top of the extension's own click, which
toggled the form shut.

## 3. Decisions made on your behalf

1. **Only HIGH-confidence inferences fill.** Medium/low are computed but never
   written. Where the profile does not settle a question (US work authorization for
   a Canadian citizen, "this country" when the job's country is unknown, highest
   education while a degree is in progress), the field stays blank AND is kept
   from the backend. Conservative: blanks are cheap, wrong legal answers are not.
2. **Questions without a country** ("Are you authorized to work in this
   country?") are answered for the JOB's country, read from the posting
   (schema.org JobPosting JSON-LD, then the ATS location line) and carried from
   the posting page to later application steps on the same host. Unknown job
   country → blank.
3. **The applicant's own sponsorship answer** applies to unscoped questions and to
   countries their status covers, never to a country they never mentioned.
4. **A single unindexed School/Degree/Graduation field** now means the PRIMARY
   education (in progress, else most recent graduation), not entry [0]. Test
   updated (`fieldMatcher.test.ts`).
5. **City/Country/Region/Postal are derived from one location or address
   string** ("Toronto, ON, Canada" → Toronto / Ontario / Canada). A bare
   "Toronto" stays MEDIUM (not filled as a country): there is a London, Ontario.
6. **Years of experience** = merged employment spans (overlaps counted once),
   high only when every row has dates. Domain-qualified questions ("software
   development experience") answer only when every job title is in that domain;
   skill-qualified ("experience with Kubernetes") never.
7. **Consent / attestation checkboxes: CORRECTED, and the policy needs you.**
   An earlier version of this note said consent boxes are never ticked because
   the selection gate blocks them. That was wrong. `checkboxIntent.ts`
   (commit d5d087f, Jobright parity) proposes "yes" for clear consent wording,
   and whether the box is then ticked depends on how confidently its label
   happens to classify: most consent boxes classify as unknown and stay
   unticked, but one that also matches a confident category gets ticked.
   Robinhood's demographic-data consent was ticked that way (bug #36). Tonight
   I fixed only the sensitive case: a demographic (EEO) box is never ticked for
   its wording. For every other consent box the behavior is unchanged and
   inconsistent; pick one policy (tick all clear application consent like
   Jobright, or none) and make the gate enforce it.
8. **Pre-filled fields are never overwritten**, even when the site guessed them
   (Workable fills Address from IP geolocation). Unchanged behavior.
9. Real-page fixtures are committed with CSS/SVG stripped (1.9 MB); the HAR
   recordings (203 MB) stay local and gitignored.
10. Nothing pushed, nothing deployed, no backend changes (the backend's
    unconditional rule answers are listed in section 5 instead).
11. **What the backend still gets.** Only abstentions on legal status (work
    authorization, sponsorship, citizenship, age) are withheld from the backend.
    Essays, opinions and skill-specific questions still go to it, so they work
    again the day the AI has credits. Every backend answer must now pass the same
    kind gate as an on-device one.
12. **Place pickers choose a place or nothing.** With several "Toronto"
    suggestions and no region/country to tell them apart, nothing is picked.
13. **Options are snapped at scan time.** A select's proposal is now the exact
    option text it will select ("I am not a veteran"), so the panel shows what
    will actually be written.
14. **Waiting for a hidden form is bounded at 10 s** and only happens on the
    page an "Apply" entry click just opened, or on an Autofill click that picked
    nothing; and there only while the page's typeable controls are mostly
    mounted-but-hidden AND (nothing is fillable yet, OR at most two controls are
    rendered). Ordinary wizard steps never wait: a wizard that keeps its other
    steps mounted but hidden would otherwise stall on pages with nothing to
    fill, such as a review page. Pages whose form is not hidden pay nothing.
15. **An Autofill click with nothing picked now always runs** (fill pass, then
    the flow: open the form, wait for it, or say "No application form found on
    this page"). Before, it returned silently. The panel's own comment says the
    button "stays live whenever a profile is loaded", so a dead click was the bug.
16. **The panel covering BambooHR's Apply button (section 5) was NOT changed.**
    Moving or shrinking the panel is a layout decision with knock-on effects on
    every site; recorded for you instead.
17. **Date controls get whole dates only.** A picker that does not state its
    format is assumed MM/DD/YYYY (react-datepicker's default, and what Ashby
    shows). A graduation year never goes into a day-precise picker: the
    profile does not know the day.
18. **A broader parent option is acceptable, a sibling is not.** With no
    "Mechatronics" option, "Engineering" may be picked (it is true);
    "Mechanical Engineering" may not (it is a different discipline).
19. **High-school questions stay blank.** Profiles hold post-secondary
    education only.

## 4. Per-ATS pass rates

Every number here is the real packaged extension in Chromium, pressing the
panel's own Autofill button, with the backend's AI returning nothing. "checks" =
expected-value checks (a value where one is expected, a BLANK where the right
answer is to abstain) plus one failure per unexpected write. All read back from
the page DOM (and the framework's own state on the framework pages).

### Final: branch HEAD `c0b43f3`, 64 cases

| ATS | cases ok | checks | rate | fills ok | correct abstentions | wrong writes |
|---|---|---|---|---|---|---|
| ashby | 11/11 | 83/83 | 100% | 51/51 | 32/32 | 0 |
| bamboohr | 5/5 | 60/60 | 100% | 38/38 | 22/22 | 0 |
| greenhouse | 13/13 | 211/211 | 100% | 128/128 | 83/83 | 0 |
| jobvite | 3/3 | 66/66 | 100% | 24/24 | 42/42 | 0 |
| lever | 11/11 | 147/147 | 100% | 81/81 | 66/66 | 0 |
| workable | 5/5 | 70/70 | 100% | 47/47 | 23/23 | 0 |
| smartrecruiters (synthetic) | 1/1 | 3/3 | 100% | 3/3 | 0/0 | 0 |
| workday | 2/2 | 21/21 | 100% | 18/18 | 3/3 | 0 |
| icims | 1/1 | 9/9 | 100% | 9/9 | 0/0 | 0 |
| taleo | 1/1 | 9/9 | 100% | 9/9 | 0/0 | 0 |
| successfactors | 1/1 | 9/9 | 100% | 9/9 | 0/0 | 0 |
| adp | 1/1 | 9/9 | 100% | 9/9 | 0/0 | 0 |
| breezy | 1/1 | 5/5 | 100% | 5/5 | 0/0 | 0 |
| bullhorn | 1/1 | 4/4 | 100% | 4/4 | 0/0 | 0 |
| rippling | 1/1 | 4/4 | 100% | 4/4 | 0/0 | 0 |
| framework (React 18 / Vue 3 / AngularJS) | 3/3 | 49/49 | 100% | 46/46 | 3/3 | 0 |
| inputs (dates, résumé upload) | 2/2 | 9/9 | 100% | 9/9 | 0/0 | 0 |
| smoke | 1/1 | 6/6 | 100% | 6/6 | 0/0 | 0 |
| **total** | **64/64** | **774/774** | **100%** | | | **0** |

Failure kinds on this run: 0 left blank where a value was expected, 0 wrong
values, 0 invented answers, 0 stray writes.

- **Read the 100% with this caveat.** Four checks accept "the right answer OR
  blank" (a blank there is a miss, never a wrong write), and all four came out
  blank in this run: Greenhouse School on ZipRecruiter and on Robinhood (the
  intermittent in section 5), "Are you currently a Twitch employee?" (No would
  be right), and Workable's "Based in Austin, TX?" (NO would be right).
- Live real pages: 42 cases, 606/606 checks. Greenhouse 12 (incl. the
  cross-origin embed), Lever 10, Ashby 10, Workable 4, BambooHR 4, Jobvite 2.
  Workday, iCIMS, Taleo, SuccessFactors, ADP, Breezy, Bullhorn and Rippling
  are synthetic fixtures served on each ATS's real host (section 5 says why).
- **Live SmartRecruiters is not in this run**: both postings answer this
  machine with HTTP 403 + a DataDome captcha (re-checked with `curl` before the
  run). Earlier the same night both passed 100%; in the run on `c7fca08` they
  were the only failures (477/499 with them, 477/477 without).
- Stability: BambooHR, the one race-prone site, passed 20/20 case runs on
  `c7fca08` (5 combined runs) and 24/24 on the commit before it.

### Held-out pages: blind first passes, then fixed

Two rounds of twelve postings drawn at random from prod `scraped_jobs` after
the main fixes, from companies not in the corpus, each first run with NO
expectations and every write reviewed by hand (`cases/real-live-heldout.mjs`
now pins them):

- **Round 1 (Ashby 4, Greenhouse 4, Lever 3, Workable 1): 14 wrong values on 6
  of 12 pages.** Ramp's graduation date picker 12/31/2026; Superhuman's and
  ZipRecruiter's graduation option chosen from a bare year that fits two;
  Twitch's and Astranis's education start/end dates set to the first job's (8
  values); Astranis's Discipline "Mechanical Engineering"; Palantir's "High
  School Name" and its graduation year. Bugs #30-#35.
- **Round 2 (Ashby 4, Greenhouse 4, Lever 4), after those fixes: 1 wrong write
  on 1 of 12 pages**: Robinhood's demographic-data consent checkbox ticked.
  Bug #36.
- Everything else written on those pages was right, including what the night's
  work targeted: US work authorization and sponsorship left blank for a
  Canadian on every US posting, dial code "+1", citizenship "Canada",
  "previously employed by Amazon / Figma / Robinhood?" answered No from the
  work history, legal-name / preferred-name cards on Lever, current company
  "Kinaxis", Lever and Ashby location pickers "Toronto", arbitration
  agreements and SMS consents left alone.
- After the fixes: round 1 12/12 cases, 172/172 checks; round 2 12/12, 125/125.

### Before/after on identical cases and expectations

The baseline build (`b9aebcc`, `main` when the night started) re-scored with
`E2E_EXT_DIR` against the SAME cases and expectations, the 40 cases that
existed before the held-out round (the branch column is `c7fca08`; `c0b43f3`
passes the same 40). Live SmartRecruiters is left out of both columns
(DataDome).

| ATS | baseline cases | baseline checks | branch cases | branch checks |
|---|---|---|---|---|
| adp | 0/1 | 8/9 (89%) | 1/1 | 9/9 (100%) |
| ashby | 2/3 | 17/19 (89%) | 3/3 | 19/19 (100%) |
| bamboohr | 1/5 | 46/60 (77%) | 5/5 | 60/60 (100%) |
| breezy | 1/1 | 5/5 (100%) | 1/1 | 5/5 (100%) |
| bullhorn | 1/1 | 4/4 (100%) | 1/1 | 4/4 (100%) |
| framework | 0/3 | 31/49 (63%) | 3/3 | 49/49 (100%) |
| greenhouse | 1/5 | 66/77 (86%) | 5/5 | 77/77 (100%) |
| icims | 0/1 | 8/9 (89%) | 1/1 | 9/9 (100%) |
| inputs | 1/2 | 6/9 (67%) | 2/2 | 9/9 (100%) |
| jobvite | 1/3 | 59/66 (89%) | 3/3 | 66/66 (100%) |
| lever | 1/4 | 49/59 (83%) | 4/4 | 59/59 (100%) |
| rippling | 1/1 | 4/4 (100%) | 1/1 | 4/4 (100%) |
| smartrecruiters (synthetic) | 1/1 | 3/3 (100%) | 1/1 | 3/3 (100%) |
| smoke | 1/1 | 6/6 (100%) | 1/1 | 6/6 (100%) |
| successfactors | 0/1 | 8/9 (89%) | 1/1 | 9/9 (100%) |
| taleo | 0/1 | 8/9 (89%) | 1/1 | 9/9 (100%) |
| workable | 1/4 | 42/60 (70%) | 4/4 | 59/59 (100%) |
| workday | 0/2 | 17/21 (81%) | 2/2 | 21/21 (100%) |
| **total** | **13/40** | **387/478 (81%)** | **40/40** | **477/477 (100%)** |

What the failures were:

| | left blank (value expected) | wrong value written | invented answer (blank expected) | stray write |
|---|---|---|---|---|
| baseline `b9aebcc` | 63 | 14 | 13 | 1 |
| branch `c7fca08` | 0 | 0 | 0 | 0 |

The baseline's 13 invented answers include "Are you legally authorized to work
in the United States?" **Yes** for a Canadian citizen (Greenhouse ×3, Workable)
and for an F-1 student (Workable, its radio and the ARIA twin), "Do you think AI will take over the
world?" **Yes**, "highest education: Bachelors" for a student whose degree is
in progress, and the degree title typed into a background-check question. Its
14 wrong values are the whole location string ("Toronto, ON, Canada",
"Ottawa, ON, Canada") in City fields, and the degree title where the school was
asked.

### Live pages through the night

The original 15 live cases, scored with the expectations as they stood at each
point (expectations were corrected during the night, e.g. Workable's
IP-prefilled Address, so the re-scored table above is the fair comparison).

| point | checks | cases |
|---|---|---|
| baseline `b9aebcc` | 170/226 (75%) | 1/15 |
| inference layer + kind gate | 185/228 (81%) | 1/15 |
| round 3 (labels, Lever, shadow listboxes) | 205/228 (90%) | 7/15 |
| round 4 (web components, place matching) | 215/226 (95%) | 10/15 |
| final `c7fca08` | 211/233 (91%); 211/211 (100%) without SmartRecruiters | 13/15; 13/13 |

### First measurement: `main` build (b9aebcc), before any change

Live pages only, SPARSE_CANADIAN profile, expectations as first written.

| ATS | cases ok | checks | rate | fills ok | correct abstentions | wrong writes |
|---|---|---|---|---|---|---|
| ashby | 1/2 | 13/15 | 87% | 5/6 | 8/9 | 0 |
| bamboohr | 0/2 | 16/28 | 57% | 5/14 | 11/14 | 0 |
| greenhouse | 0/3 | 44/52 | 85% | 23/28 | 21/24 | 0 |
| jobvite | 0/1 | 28/31 | 90% | 6/8 | 22/23 | 0 |
| lever | 0/3 | 43/53 | 81% | 13/22 | 30/31 | 0 |
| smartrecruiters | 0/2 | 6/20 | 30% | 0/14 | 6/6 | 0 |
| workable | 0/2 | 20/27 | 74% | 8/12 | 12/13 | 1 |
| **total** | **1/15** | **170/226** | **75%** | | | |

Wrong answers the baseline actually wrote on live pages (all now pinned by tests,
see section 2):
- "Are you legally authorized to work in the United States?" → **Yes** for a
  Canadian citizen (Greenhouse ×2, Workable). Country-blind.
- "Do you anticipate having any challenges with clearing a background check?"
  → the applicant's degree title (Lever).
- "Do you think AI will take over the world?" → **Yes** (Ashby).
- "If 'Other' selected for School Name, please indicate here" → the school (Greenhouse).
- "Where did you complete your undergraduate degree?" → the degree title, not the school.
- City → "Toronto, ON, Canada" (whole location string), Address → the location string (BambooHR).
- (Not our bug, recorded so nobody chases it: Workable's Address showed
  "Gatineau, Canada". Workable pre-fills that field from the visitor's IP
  geolocation; the capture taken with NO extension loaded already contains it.
  The extension leaves non-empty fields alone, by design.)

## 5. Needs you / needs manual verification

**Your decisions / actions**
1. **Review and merge** `night/deterministic-autofill` (26 commits on top of
   `b9aebcc`, the last one adding this file; local only, not pushed). Then
   `cd chrome-extension && node build.mjs` and **reload the unpacked
   extension** at chrome://extensions (Chrome caches it).
2. **Backend bug, not touched (out of scope tonight):**
   `backend/routers/fill.py::_raw_rule_based_answer` answers any "sponsorship"
   question "No", and any "authorized to work" / "18 or older" question "Yes",
   unconditionally. `answer_gate` only checks the POLARITY of the stored
   statement, never the country, so "Authorized to work in Canada" passes a
   "Yes" to "authorized to work in the United States?". The extension now keeps
   those questions away from the backend when it has abstained on them, but any
   other client of `/api/fill` still gets the guess. Recommend deleting those
   three rules (derived_facts already computes age from the DOB).
3. **Decision #7 needs your call**: consent checkboxes are ticked or not
   depending on incidental classification today (only the demographic case is
   fixed). Choose "tick clear application consent" or "never tick", and I would
   enforce it in `shared/selection.ts`.
4. **The panel covers the site's own Apply button on BambooHR** at 1366×900 (a
   very common laptop size). The auto-mounted panel is a 380 px overlay on the
   right, and BambooHR's "Apply for This Job" sits in the right column
   underneath it: a real pointer click lands on the panel. Our own Autofill
   clicks Apply programmatically, so the extension flow works, but a user who
   wants to open the form by hand has to close the panel first. Options: push
   the page left (margin on `<html>`) while the panel is open, start collapsed
   on job-description pages, or leave it. Not changed tonight (decision #16).
   Seen and measured with `elementFromPoint` on the live posting.

**Needs manual verification (I could not test these for real)**
- **Workday application steps.** The real form sits behind account creation, and
  creating accounts on employers' tenants is an outward action I did not take.
  Covered only by the synthetic fixtures (incl. shadow DOM) and the captured
  sign-in wall. The Workday-specific code paths (prompt dropdowns, date
  spinbuttons, "How did you hear" multiselect, Add-row sections) were not
  exercised against a live tenant tonight. The new URL-based job-country
  detection is unit-tested on real Workday URLs.
- **iCIMS and Taleo** application forms (login walls; the app has no active Taleo
  listings). Synthetic fixtures only. **SuccessFactors and ADP**: synthetic only.
- **SmartRecruiters**: both live pages passed 100% earlier in the night (City
  "Toronto, Ontario, Canada", dial code "+1"; ServiceNow again 12/12 in the
  second-to-last full run). By the final run both postings answer this machine
  with **HTTP 403 and a DataDome captcha page** (checked with a plain `curl` GET
  each), so their forms never render and every check reads as missing. That is
  bot protection reacting to repeated automated runs from one IP, not an
  extension failure. Re-check both by hand once from a normal browser.
- **Real profiles with unusual work-authorization text.** The parser handles
  citizen / permanent resident / green card / H-1B / TN / OPT / F-1 / study and
  work permits / "authorized to work in X [and Y]" / bare yes-no. Anything else
  abstains (blank), which is safe but may leave fields empty.
- The panel's gap list still shows abstained fields as unanswered, which is
  intended, but I did not review the panel UX visually.
- **Misses the held-out pages showed (blank, never wrong; not fixed):** Ashby's
  education Start/End month-year selects and "Still Student?"; Gecko's Ashby
  location combobox (filled on the other Ashby pages); Workable TSA's
  city/postcode/country sub-fields; "Based in Austin, TX?" (should be NO for a
  Toronto applicant); graduation-range options ("January - June 2027") could be
  matched when the profile has a month (it usually has only the year).
- **Known intermittent (not fixed):** Greenhouse's async School react-select
  (ZipRecruiter, Robinhood) sometimes ignores the extension's interaction in
  combined runs, about 1 in 5 ("Couldn't open the dropdown"): the field stays
  blank, never wrong. Run alone it filled 8/8. A probe WITHOUT the extension
  hit the same thing once in 13 tries (the widget sent no search request at
  all), so it looks like page readiness, not matching. No reliable repro, so no
  fix I could verify.
- **Partial answers (right but incomplete):** "Where do you live? (City and
  State/Province)" gets "Toronto" (no province); a graduation asked as "(Term &
  Year)" gets "2027".

**Housekeeping**
- Live e2e cases use real postings that will expire. Re-capture with
  `node test/e2e/capture.mjs` (edit `TARGETS`) and update `cases/real-live*.mjs`.
- HAR recordings of every captured page (203 MB, full-fidelity offline replay)
  are in `chrome-extension/test/e2e/results/har/` (gitignored, local only).
- The result JSON/text of every run quoted in section 4 is in
  `chrome-extension/test/e2e/results/` (gitignored): `final5-all` (final, 64
  cases), `final3-all` (with live SmartRecruiters), `heldout-1` and
  `heldout2-1` (the two blind held-out passes), `baseline-rescored` (b9aebcc on the same cases),
  `baseline-live` (first measurement), `round3-live`, `round4-live`.
- Nothing was pushed, deployed, or sent anywhere. The only network writes were
  to the local fake backend.
