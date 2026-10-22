// @vitest-environment-options {"url": "https://jobs.ashbyhq.com/qbank/00000000-0000-0000-0000-000000000000/application"}
/**
 * Round 8's new Ashby boards (2026-10-22), read blind live. Labels and options
 * verbatim from each posting (GET and GraphQL queries only). Tested through
 * the scan path.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import * as P from "./e2e/profiles.mjs";
import type { UserApplicationProfile } from "../src/shared/types";
import { stubLayout } from "./helpers/layout";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());
beforeEach(() => {
  document.body.innerHTML = "";
});

/** Personas from profiles.mjs, and variants a test adds. */
const extra: Record<string, UserApplicationProfile> = {};
const persona = (who: string) => extra[who] ?? ((P as Record<string, unknown>)[who] as UserApplicationProfile);
const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const YN = ["Yes", "No"];

/** One question on a posting of `company`: the scanner's proposed answer (null when blank). */
function ask(who: string, label: string, options: string[], company: string, country = "US"): string | null {
  const control = options.length
    ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>`
    : `<input type="text" id="q0" name="q0">`;
  document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(label)}</label>${control}</div></form>`;
  setResolveContext({ jobCountry: country, jobCity: null, jobPlaces: null, company });
  const f = scanPage(persona(who), true).fields.find((x) => x.label.startsWith(label.slice(0, 30)));
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return f?.proposedValue ?? null;
}

describe("the hiring company's own employees, named every which way (1Password, Amplify, Pebl)", () => {
  it("a name starting with a digit; employed or engaged WITH the company", () => {
    expect(ask("COMPLETE_CANADIAN", "Are you a former 1Password employee?", YN, "1Password", "CA")).toBe("No");
    expect(ask("US_F1_STUDENT", "Have you previously been employed with Amplify?", YN, "Amplify")).toBe("No");
    expect(ask("US_GREENCARD_STUDENT", "Are you currently (or previously) engaged with Pebl or its affiliate partners/clients as an employee, supported employee or any engagement type?", YN, "Pebl", "CA")).toBe("No");
  });
  it("currently working with the hiring company is employment there", () => {
    expect(ask("US_F1_STUDENT", "Are you currently working with Amplify Education, Inc.?", YN, "Amplify")).toBe("No");
  });
  it("working with a tool is no employer", () => {
    expect(ask("US_F1_STUDENT", "Are you currently working with Python?", YN, "Amplify")).toBeNull();
  });
});

describe("a box that only mentions the self-ID topics (1Password)", () => {
  it("gets no demographic answer", () => {
    const label =
      "Helping you bring your best self to the recruitment process means a lot to us. Other than your ethnicity, gender, and disability status (survey below), is there anything you would like to share with us, or information to help us accommodate you?";
    document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(label)}</label><textarea id="q0" name="q0"></textarea></div></form>`;
    setResolveContext({ jobCountry: "CA", jobCity: null, jobPlaces: null, company: "1Password" });
    const f = scanPage(persona("COMPLETE_CANADIAN"), true).fields.find((x) => x.label.startsWith("Helping you"));
    setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
    expect(f?.proposedValue ?? null).toBeNull();
  });
});

describe("Pebl's start and duration", () => {
  const START = "This Internship requires you to be able to start no later than Monday January 4th, 2027. Are you able to start on this date?";
  it("a start the posting sets, against the stated earliest start", () => {
    // Earliest starts: 2027-05-24 (US_GREENCARD_STUDENT), 2027-01-04 (UK_SENIOR).
    expect(ask("US_GREENCARD_STUDENT", START, YN, "Pebl", "CA")).toBe("No");
    expect(ask("UK_SENIOR", START, YN, "Pebl", "CA")).toBe("Yes");
  });
  it("the posting's own duration is accepted by applying (decision 86)", () => {
    expect(ask("US_GREENCARD_STUDENT", "This is a full-time 12 week internship. Are you able to commit to the full duration?", YN, "Pebl", "CA")).toBe("Yes");
  });
});

describe("Jane's form (live, round 8)", () => {
  it("a follow-up for those who chose Other gets no self-ID answer", () => {
    // The gender list was answered Non-Binary; "Non-binary" and "Two or More
    // Races" were also typed into the two optional describe boxes.
    for (const label of ["If you selected “Other\", you can describe your gender in your own words below (optional).", "If you selected “Another racial or ethnic identity not listed”, you can describe your identity in your own words below (optional)."]) {
      expect(ask("MONTREAL_CHANGER", label, [], "Jane", "US"), label).toBeNull();
    }
  });
  it("a work-eligibility heading over its own Yes and No answers is the question", () => {
    const opts = ["Yes, I’m eligible to work in the United States", "No, I'm not eligible"];
    expect(ask("MONTREAL_CHANGER", "Work Eligibility in the United States 🇺🇸", opts, "Jane", "US")).toBe(opts[1]);
    expect(ask("US_VETERAN", "Work Eligibility in the United States 🇺🇸", opts, "Jane", "US")).toBe(opts[0]);
  });
});

describe("Regent's salary and start (live, round 8)", () => {
  it("accepting the stated salary and terms is the applicant's call", () => {
    // BERLIN_STAFF got Yes. The posted pay is not in the question; whether
    // it suits is theirs, as with any pay question (Nuro, round 4).
    expect(ask("BERLIN_STAFF", "Do you accept the position at the salary & terms stated, if offered?", YN, "Regent", "US")).toBeNull();
  });
  it("a start asked as immediately is the earliest start's, within a week", () => {
    // Earliest starts: 2027-02-01 (BERLIN_STAFF), 2026-10-19, passed
    // (BOOTCAMP_CAREER_GAP). Sent to the AI, which could not answer.
    expect(ask("BERLIN_STAFF", "Are you available to start immediately? If not, when?", YN, "Regent", "US")).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", "Are you available to start immediately? If not, when?", YN, "Regent", "US")).toBe("Yes");
  });
});

describe("1Password's source list", () => {
  it("what brought someone to the posting is how they heard of it", () => {
    const opts = ["Agency", "Black Professionals in Tech Network", "Blog / news article", "Campus event / job portal", "Canadian Cybersecurity Jobs", "CLOC", "Dribble", "Facebook", "Glassdoor", "hired.com", "IDPro", "Indeed", "Instagram", "Ladders", "LinkedIn Jobs", "LinkedIn Social Post", "Newsletter", "Other", "Otta", "Previously worked at 1Password", "Queertech", "Word of mouth", "X (formerly Twitter)", "Youtube"];
    // COMPLETE_CANADIAN heard through LinkedIn. Sent to the AI live.
    expect(ask("COMPLETE_CANADIAN", "What brought you to this job posting", opts, "1Password", "CA")).toBe("LinkedIn Jobs");
  });
});

describe("Amplify's school-district employment", () => {
  const LEA = "Do you currently work or have you in the past 24 months worked for a local educational agency/school district or a state education agency?";
  it("a school district or state education agency is a government employer", () => {
    // US_F1_STUDENT's only job: Acme Robotics. Sent to the AI live.
    expect(ask("US_F1_STUDENT", LEA, YN, "Amplify")).toBe("No");
  });
  it("someone who worked for one answers it", () => {
    const p = { ...persona("US_F1_STUDENT"), experience: [{ company: "Springfield Public Schools", title: "Student Teacher", startDate: "January 2025", endDate: "May 2025", description: "" }] };
    extra.LEA_TEACHER = p as UserApplicationProfile;
    expect(ask("LEA_TEACHER", LEA, YN, "Amplify")).toBeNull();
  });
});

describe("a degree asked at one level (Barnes & Thornburg, Ashby bank, round 8)", () => {
  it("a bootcamp's certificate takes no level's Other (decision 96 is the row's own Degree list)", () => {
    // BOOTCAMP_CAREER_GAP: a bootcamp certificate and a high school diploma.
    // The bank's re-run gave "Other", "Other bachelor's degree", "Other".
    const lists: Array<[string, string[]]> = [
      ["Law Degree", ["JD", "LLM", "LLB", "Other"]],
      ["Undergradute Degree", ["No degree completed", "Some college/undergraduate coursework", "Associate Degree", "Bachelor of Arts (B.A.)", "Bachelor of Science (B.S.)", "Other bachelor's degree"]],
      ["Graduate Degree", ["MA", "MS", "MBA", "MFA", "MEd", "MSW", "MPH", "Ph.D.", "Ed.D.", "M.D.", "Pharm.D.", "Other"]],
    ];
    for (const [label, opts] of lists) expect(ask("BOOTCAMP_CAREER_GAP", label, opts, "Barnes & Thornburg"), label).toBeNull();
  });
  it("the row's own Degree list still takes its Other", () => {
    expect(ask("BOOTCAMP_CAREER_GAP", "Degree", ["Associate Degree", "Bachelor's Degree", "Master's Degree", "Phd", "Other"], "Quora")).toBe("Other");
  });
});
