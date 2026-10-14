/**
 * Question bank 3 (74 more Greenhouse postings, 2026-10-05): answers read by
 * hand and found wrong for a persona. Each test fails on the code before its fix.
 */
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { stubLayout } from "./helpers/layout";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import type { UserApplicationProfile } from "../src/shared/types";
import * as P from "./e2e/profiles.mjs";

const YES_NO = ["Yes", "No"];
type Persona = keyof typeof P;
type Ctx = { jobCountry: string | null; company: string; jobCity?: string | null; jobPlaces?: string[] | null };

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-05T12:00:00Z"));
});
afterAll(() => {
  restore();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

const esc = (t: string): string => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** One question as the bank renders it (a select for a list, a text box
 *  otherwise), scanned for a persona: the proposed answer, "abstain" or "none". */
function ask(who: Persona, label: string, options?: string[], ctx: Ctx = { jobCountry: "US", company: "" }, tag = "input"): string {
  const control = options
    ? `<select id="q0"><option value="">Select...</option>${options.map((o, j) => `<option value="${j}">${esc(o)}</option>`).join("")}</select>`
    : tag === "textarea" ? `<textarea id="q0"></textarea>` : `<input type="text" id="q0">`;
  document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(label)}</label>${control}</div></form>`;
  setResolveContext({ jobCountry: ctx.jobCountry, company: ctx.company, jobCity: ctx.jobCity ?? null, jobPlaces: ctx.jobPlaces ?? null });
  const { fields } = scanPage(P[who] as unknown as UserApplicationProfile, true);
  const f = fields[0];
  return f?.proposedValue ?? (f?.deviceAbstained ? "abstain" : "none");
}

const US: Ctx = { jobCountry: "US", company: "" };

describe("export controls are not a US-citizenship question (Asana, Intercom)", () => {
  it("a US citizen is no citizen of a sanctioned country", () => {
    const asana = "Asana is seeking the below information for the exclusive and limited purpose of complying with U.S. export control laws given that the position for which you are applying may require access to technology subject to those laws. We will not use the information that you provide for any other purpose. Are you a citizen or legal permanent resident of Cuba, Iran, North Korea, Syria or Ukraine (Crimea region)?";
    expect(ask("BOOTCAMP_CAREER_GAP", asana, YES_NO)).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", "US Export Control laws 2026: Are you a citizen, national or permanent resident of Iran, Cuba, North Korea or Syria?", YES_NO)).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", "US Export Control laws 2026: Do you hold an ADDITIONAL citizenship, nationality, or permanent residency of any other country that is NOT Iran, Cuba, Syria or North Korea?", ["N/A", "Yes", "No"])).not.toBe("Yes");
  });
});

describe("permanent or unrestricted work authorization (Airtable, MyFundedFutures, Everlaw, Warp)", () => {
  const QS: Array<[string, string[] | undefined]> = [
    ["Do you have permanent work authorization to work in the U.S?", undefined],
    ["Are you currently authorized to work in the United States without restriction?", YES_NO],
    ["Are you authorized to work in the United States without restrictions?", YES_NO],
  ];
  it("a visa is not permanent or unrestricted; citizenship is", () => {
    for (const [q, o] of QS) {
      // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
      expect(ask("US_H1B_SENIOR", q, o), q).toBe("abstain");
      expect(ask("US_OPT_ANALYST", q, o), q).toBe("abstain");
      expect(ask("BOOTCAMP_CAREER_GAP", q, o), q).toBe("Yes");
    }
  });
  it("either of two countries named (Warp): a Canadian citizen has it", () => {
    const q = "Do you have permanent authorization to work for Warp in the U.S. or Canada?";
    expect(ask("COMPLETE_CANADIAN", q, YES_NO)).toBe("Yes");
    expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO)).toBe("Yes");
    // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
    expect(ask("US_H1B_SENIOR", q, YES_NO)).toBe("abstain");
    expect(ask("BERLIN_STAFF", q, YES_NO)).toBe("No");
  });
  it("based in either of two countries: where they live (Warp)", () => {
    const q = "If hired by Warp, will you be based in the U.S. or Canada?";
    expect(ask("US_H1B_SENIOR", q, YES_NO)).toBe("Yes");
    expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO)).toBe("Yes");
    expect(ask("COMPLETE_CANADIAN", q, YES_NO)).toBe("Yes");
  });
});

describe("a question about the role or a kind of experience is no fact to copy (Airtable, Hootsuite, Hudl, Wikimedia)", () => {
  it("how they use AI in their current role is not their title", () => {
    expect(ask("US_H1B_SENIOR", "How are you using AI today in your current role? If applicable, show us your last AI experiment.")).not.toBe("Senior Data Engineer");
  });
  it("experience of one kind is not the whole work history", () => {
    const qs: Array<[string, string]> = [
      ["Do you have any SaaS or software sales experience? If so, how many years and in which company?", "input"],
      ["Please tell us about any experience you have within sport?", "input"],
      ["Tell us about your experience in product marketing and the work you have done to successfully drive awareness and engagement with a product. (Limit: two paragraphs)", "textarea"],
    ];
    for (const [q, tag] of qs) {
      expect(ask("US_H1B_SENIOR", q, undefined, US, tag), q).not.toMatch(/Senior Data Engineer/);
    }
  });
});

describe("India is not Indiana (Starburst, Doximity)", () => {
  const STATES = ["Alabama", "Alaska", "Arizona", "Illinois", "Indiana", "Iowa", "Massachusetts", "Washington", "Other"];
  it("a list of US states has nothing for Bengaluru", () => {
    expect(ask("INDIA_NEW_GRAD", "What location do you intend to work out of?", STATES)).not.toBe("Indiana");
    expect(ask("INDIA_NEW_GRAD", "What location are you planning to work from?", ["HQ (San Francisco, CA)", ...STATES])).not.toBe("Indiana");
  });
});

describe("a city named with a comma after it is still a place (AssemblyAI)", () => {
  it("hybrid in NYC, for people elsewhere who will not move: No", () => {
    const q = "This role is hybrid in NYC, 2 days per week in office. Does that work for you?";
    expect(ask("US_H1B_SENIOR", q, YES_NO)).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO)).toBe("No");
    expect(ask("US_OPT_ANALYST", q, YES_NO)).toBe("Yes");
  });
});

describe("a yes/no question in a text box gets no fact (CircleCI)", () => {
  it("not a location, not a salary", () => {
    expect(ask("US_H1B_SENIOR", "Are you located in and willing to work in the location where the job is posted?")).not.toMatch(/Seattle/);
    expect(ask("US_H1B_SENIOR", "Are your salary expectations for this role within the posted salary band (if applicable)?")).not.toMatch(/185/);
  });
});

describe("how they heard: the channel stated, never another (Flipp, Geotab)", () => {
  it("LinkedIn is not a recruiter's outreach", () => {
    expect(ask("COMPLETE_CANADIAN", "Before applying, how did you first hear about Flipp?", ["I was already familiar with the brand","Job board","Social media","Referral","Content (podcast/blog/video)","Event","A recruiter from Flipp/Shopfully contacted me directly, such as via LinkedIn message or email, before I applied or became aware of the role.","Other (please specify)"])).not.toMatch(/recruiter/i);
  });
  it("LinkedIn is not Handshake", () => {
    expect(ask("COMPLETE_CANADIAN", "How did you hear about this job opportunity?", ["Campus Event", "University/College Job Board", "Campus Newsletter", "Handshake", "Other"])).not.toBe("Handshake");
  });
});

describe("the highest degree level currently pursued (CTC)", () => {
  const OPTS = ["High School Diploma", "Associate's Degree", "Bachelor's Degree", "Master's Degree", "Doctor of Philosophy (PhD)"];
  it("the degree in progress; graduates pursue none", () => {
    expect(ask("COMPLETE_CANADIAN", "What is the highest degree level you are currently pursuing?", OPTS)).toBe("Bachelor's Degree");
    expect(ask("US_H1B_SENIOR", "What is the highest degree level you are currently pursuing?", OPTS)).not.toBe("Master's Degree");
    expect(ask("INDIA_NEW_GRAD", "What is the highest degree level you are currently pursuing?", OPTS)).not.toBe("Bachelor's Degree");
  });
  it("the most recently COMPLETED is not one in progress (NISC)", () => {
    expect(ask("COMPLETE_CANADIAN", "What is your most recently completed form of education?", ["High School Equivalency","High School","Associate's Degree","Bachelor's Degree","Masters's Degree","Other"])).not.toBe("Bachelor's Degree");
  });
});

describe("referred by an employee, for someone who said they were referred (LaunchDarkly, Blue Moon Metals)", () => {
  it("is Yes", () => {
    expect(ask("BERLIN_STAFF", "Were you referred to this role by a current employee?", YES_NO)).toBe("Yes");
    expect(ask("BERLIN_STAFF", "Were you referred by a Blue Moon Metals employee?", YES_NO, { jobCountry: "NO", company: "Blue Moon Metals" })).toBe("Yes");
    expect(ask("US_H1B_SENIOR", "Were you referred to this role by a current employee?", YES_NO)).toBe("No");
  });
});

describe("a salary in another currency than asked (Clearway, NISC, A Thinking Ape)", () => {
  it("euros are not dollars", () => {
    expect(ask("BERLIN_STAFF", "What is your closest desired base salary expectation?", ["$40,000","$50,000","$60,000","$70,000","$80,000","$90,000","$100,000","$110,000","$120,000","$130,000","$140,000","$150,000","$160,000","$170,000","$180,000","$190,000","$200,000","$210,000","$220,000","$230,000","$240,000","$250,000 or more"])).not.toMatch(/\$/);
    expect(ask("BERLIN_STAFF", "Target Compensation", ["less than 25k","26-30k","31-35k","36-40k","41-45k","46-50k","51-55k","56-60k","61-65k","66-70k","71-75k","76-80k","81-85k","86-90k","91-95k","96-100k","101-110k","111-120k","121-130K","131-140K","141-150K","151-160K","161-170K","171K+"])).not.toMatch(/k$/i);
  });
  it("US dollars are not Canadian", () => {
    expect(ask("US_H1B_SENIOR", "What is your desired salary (CAD$)?", undefined, { jobCountry: "CA", company: "" })).not.toMatch(/185/);
    expect(ask("COMPLETE_CANADIAN", "What is your desired salary (CAD$)?", undefined, { jobCountry: "CA", company: "" })).toBe("95000");
  });
});

describe("a preference among the company's offices is not where they live (Hudl)", () => {
  it("is left to them", () => {
    expect(ask("US_H1B_SENIOR", "What is your preferred office location?")).not.toMatch(/Seattle/);
  });
});

describe("in-person presence in other words (Vestmark)", () => {
  it("'come into the Wakefield, MA office five days' is No from Seattle or Denver for someone who will not move", () => {
    const q = "Are you willing and able to come into the Wakefield, MA office five days per week?";
    expect(ask("US_H1B_SENIOR", q, YES_NO)).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO)).toBe("No");
  });
});

describe("a notice shown as a text box is no question (Vestmark's California notice)", () => {
  it("gets nothing", () => {
    expect(ask("US_H1B_SENIOR", "California law requires that we provide you this notice about the collection and use of your personal information. Our Privacy Policy (https://www.vestmark.com/privacy-policy) describes the categories of personal information that Vestmark, Inc. (“Company”, “we”, “us” and “our”) collects about California residents who apply or are recruited for a job with us (“candidates”), the purposes for which we use that information, and how to exercise your rights with respect to that information. For purposes of this notice, “personal information” has the meaning given in the California Consumer Privacy Act of 2018 (the “CCPA”), as amended by the California Privacy Rights Act of 2020, but excludes information exempted from the CCPA’s scope. This notice does not create or form part of any contract for employment or otherwise. If you have questions about this notice, please contact privacy@vestmark.com or Vestmark, Inc., 100 Quannpowitt Parkway, Suite 205, Wakefield, MA 01880 Attn: Data Protection Officer.")).not.toMatch(/University/);
  });
});

describe("visa examples do not name the job's country (New Relic, Tokyo)", () => {
  it("a US citizen needs sponsorship in Japan", () => {
    expect(ask("BOOTCAMP_CAREER_GAP", "Do you now or will you in the future require sponsorship for or assistance with an employment visa, authorization, or permit to work for or continue to work for New Relic in this role (e.g. U.S. F-1, H-1B, TN, E-3, or other non-immigrant visa category)?", YES_NO, { jobCountry: "JP", company: "New Relic" })).not.toBe("No");
  });
});

describe("follow-ups the bank-3 fixes must not break", () => {
  it("who referred them is never their own name (Renaissance)", () => {
    expect(ask("BERLIN_STAFF", "If you were referred by a current employee, what is the employee's full name?")).toBe("abstain");
  });
  it("the countries where the right is unrestricted are countries (Elastic)", () => {
    expect(ask("COMPLETE_CANADIAN", "In what countries do you have the unrestricted right to work?")).toBe("Canada");
    expect(ask("BOOTCAMP_CAREER_GAP", "In what countries do you have the unrestricted right to work?")).toBe("United States");
    expect(ask("US_H1B_SENIOR", "In what countries do you have the unrestricted right to work?")).not.toMatch(/^(Yes|No)$/);
  });
  it("the office closest to where they live is theirs to name (Tubi)", () => {
    const OPTS = ["Chicago", "London", "Los Angeles", "Mexico City", "New York", "San Francisco (HQ)", "Seattle", "Tempe", "I am not close to an office location"];
    expect(ask("US_H1B_SENIOR", "Which office hub are you closest to? Please select the office location closest to your current city of residence.", OPTS)).toBe("Seattle");
  });
});

describe("referral words that are no referral (Asana)", () => {
  it("the name you would like to be referred to as is still your name", () => {
    expect(ask("BERLIN_STAFF", "[Optional] Preferred Full Name (The name that you would like to be referred to as. Please input your legal name in the first and last name fields above)")).toBe("Jürgen Weiß");
  });
  it("a referred applicant's referrer is never left to a guess (Fivetran)", () => {
    expect(ask("BERLIN_STAFF", "If you were referred for this role, please share the name of the employee that referred you")).toBe("abstain");
  });
});

describe("a school that carries every word of the name beats a generic one (Palantir, regression 2026-10-05)", () => {
  it("Imperial College London is not ambiguous beside University of London", async () => {
    const { snapSchool } = await import("../src/content/schoolMatch");
    const opts = ["Birkbeck, University of London", "Imperial College London - ICL", "King's College London", "University College London - UCL", "University of London", "Other - School Not Listed"];
    expect(snapSchool(opts, "Imperial College London")).toBe("Imperial College London - ICL");
    expect(snapSchool(opts, "University of London")).toBe("University of London");
  });
});

describe("a stated LinkedIn takes LinkedIn's own jobs (Samsara)", () => {
  it("not an alumni group, a post or a message", () => {
    const OPTS = ["Samsara Careers Site", "LinkedIn Jobs", "LinkedIn Post", "LinkedIn InMail", "Message from a Recruiter or Hiring Manager", "Indeed", "Glassdoor", "School Alumni Job Board or LinkedIn Group", "Company Alumni Job Board or LinkedIn Group"];
    expect(ask("COMPLETE_CANADIAN", "How did you hear about this opportunity?", OPTS)).toBe("LinkedIn Jobs");
  });
});
