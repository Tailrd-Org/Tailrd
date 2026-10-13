/**
 * The deterministic question resolver: screening-question SHAPES answered from
 * derived profile facts, rendered into what the field accepts. Abstentions are
 * asserted as carefully as answers: an abstention is what keeps a field blank
 * instead of filled with a confident guess.
 */
import { describe, expect, it } from "vitest";
import { profileFacts } from "../src/content/profileFacts";
import { countryNamedIn, formatDateFor, resolveQuestion, type QuestionInput } from "../src/content/questionResolver";
import type { AnswerKind } from "../src/content/answerKind";
import type { ControlType, FieldCategory, UserApplicationProfile } from "../src/shared/types";
import { SPARSE_CANADIAN, TEST_TODAY } from "./fixtures/profiles";

const YES_NO = ["Yes", "No"];

function ask(
  label: string,
  opts: { options?: string[]; controlType?: ControlType; kind?: AnswerKind; category?: FieldCategory; inputType?: string; placeholder?: string } = {},
  profile: UserApplicationProfile = SPARSE_CANADIAN,
  ctx = { jobCountry: null as string | null, company: "" }
) {
  const q: QuestionInput = {
    label,
    controlType: opts.controlType ?? (opts.options ? "radioGroup" : "text"),
    options: opts.options,
    category: opts.category ?? "unknown",
    kind: opts.kind ?? (opts.options ? (opts.options.length === 2 && opts.options[0] === "Yes" ? "boolean" : "choice") : "text"),
    inputType: opts.inputType,
    placeholder: opts.placeholder,
  };
  return resolveQuestion(q, profileFacts(profile, TEST_TODAY), profile, ctx);
}

const value = (r: ReturnType<typeof ask>) => (r && r.status === "answer" ? r.value : r?.status ?? null);

describe("M-3 (decide-b review): a condition word named like an Object.prototype key", () => {
  it("does not crash the resolver", () => {
    for (const w of ["constructor", "__proto__", "toString", "valueOf"]) {
      expect(() => ask(`If you ${w} not in Canada, are you willing to relocate?`, { options: YES_NO })).not.toThrow();
    }
  });
});

describe("work authorization, country-aware", () => {
  it("Canadian citizen: Canada Yes, United States ABSTAIN (never Yes)", () => {
    expect(value(ask("Are you legally authorized to work in Canada?", { options: YES_NO }))).toBe("Yes");
    expect(value(ask("Are you legally authorized to work in the United States?", { options: YES_NO }))).toBe("abstain");
    expect(value(ask("Are you authorized to work in the U.S.?", { options: YES_NO }))).toBe("abstain");
  });

  it("'this country' follows the job's country, and abstains when it is unknown", () => {
    expect(value(ask("Are you authorized to work in this country?", { options: YES_NO }))).toBe("abstain");
    expect(value(ask("Are you authorized to work in this country?", { options: YES_NO }, SPARSE_CANADIAN, { jobCountry: "CA", company: "" }))).toBe("Yes");
    expect(value(ask("Are you authorized to work in this country?", { options: YES_NO }, SPARSE_CANADIAN, { jobCountry: "US", company: "" }))).toBe("abstain");
  });

  it("'eligible to work in Canada without sponsorship?' is authorized AND no sponsorship", () => {
    expect(value(ask("Are you eligible to work in Canada without sponsorship?", { options: ["YES", "NO"] }))).toBe("YES");
    expect(value(ask("Are you eligible to work in the US without sponsorship?", { options: ["YES", "NO"] }))).toBe("abstain");
  });

  it("sponsorship: the applicant's stated No covers their own country, not another", () => {
    const p = { ...SPARSE_CANADIAN, workAuthorization: "Authorized to work in Canada", requiresSponsorship: "No" };
    expect(value(ask("Will you now or in the future require sponsorship?", { options: YES_NO }, p))).toBe("No");
    expect(value(ask("Will you now or in the future require visa sponsorship to work in the United States?", { options: YES_NO }, p))).toBe("abstain");
  });

  it("a stated 'Yes' to sponsorship is answered Yes", () => {
    const p = { ...SPARSE_CANADIAN, location: "Boston, MA", workAuthorization: "F-1 student visa", requiresSponsorship: "Yes" };
    expect(value(ask("Will you require sponsorship for employment visa status (e.g. H-1B)?", { options: YES_NO }, p))).toBe("Yes");
  });

  it("renders into sentence options by polarity", () => {
    const opts = ["Yes, I am authorized to work in Canada", "No, I am not authorized to work in Canada"];
    expect(value(ask("Are you legally entitled to work in Canada?", { options: opts }))).toBe(opts[0]);
  });

  it("free-text authorization gets the statement only for the country it covers", () => {
    expect(value(ask("Please describe your work authorization status in Canada", { kind: "text" }))).toBe("Canadian citizen");
    expect(value(ask("Please describe your work authorization status in the United States", { kind: "text" }))).toBe("abstain");
  });

  it("WHICH sponsorship is needed is no profile answer, never the authorization statement (Brex, live 2026-10-03)", () => {
    const label = "If you're not authorized to work at the stated location, what sponsorship would you require for the role?";
    // With the US answer on file the statement "covers" the US: that is how it got written.
    const p = { ...SPARSE_CANADIAN, authorizedUS: "No", authorizedCanada: "Yes" };
    const r = ask(label, { kind: "text" }, p, { jobCountry: "US", company: "Brex" });
    expect(value(r)).toBe("abstain");
    expect(r && r.status === "abstain" && r.blockBackend).toBe(true);
    expect(value(ask("What type of visa sponsorship will you require?", { kind: "text" }, p))).toBe("abstain");
  });
});

describe("explicit per-country authorization beats the general sponsorship answer (live 2026-10-03)", () => {
  // What a Canadian who finished onboarding has: not authorized in the US, and
  // "No" to sponsorship, meaning Canada.
  const P = { ...SPARSE_CANADIAN, requiresSponsorship: "No", authorizedUS: "No", authorizedCanada: "Yes" };
  const US = { jobCountry: "US" as string | null, company: "" };
  it("a US sponsorship question is Yes: not authorized there", () => {
    // Hermeus on Lever got "No, I do not require sponsorship".
    expect(value(ask("Do you require visa sponsorship for employment in the USA?✱", { options: ["Yes, I require sponsorship", "No, I do not require sponsorship"] }, P))).toBe("Yes, I require sponsorship");
    // Robinhood, ZipRecruiter, Twitch (all Greenhouse) got "No".
    expect(value(ask("Will you now (or in the future) require visa sponsorship in order to work in the US?*", { options: YES_NO }, P))).toBe("Yes");
    expect(value(ask("Will you now, or in the future, require sponsorship (i.e. H-1B visa, etc.) to legally work in the U.S.?*", { options: YES_NO }, P))).toBe("Yes");
    expect(value(ask("Your response is mandatory when applying for a U.S.-based position. Do you need, or will you need in the future, any immigration related support or sponsorship from Amazon to work in the U.S.?", { options: YES_NO }, P))).toBe("Yes");
  });
  it("an unscoped one follows the job's country, and abstains while it is unknown", () => {
    const q = "Will you now or will you in the future require employment visa sponsorship?✱";
    expect(value(ask(q, { options: YES_NO }, P, US))).toBe("Yes");
    expect(value(ask(q, { options: YES_NO }, P, { jobCountry: "CA", company: "" }))).toBe("No");
    expect(value(ask(q, { options: YES_NO }, P))).toBe("abstain");
  });
  it("Canada is still answered from the citizenship (and citizenship questions keep reading it)", () => {
    expect(value(ask("Will you require sponsorship to work in Canada?", { options: YES_NO }, P))).toBe("No");
    expect(value(ask("In which country/region do you have citizenship?*", { options: ["Brazil", "Canada", "United States"], kind: "choice" }, P))).toBe("Canada");
  });
  it("whether work authorization is NEEDED is the inverse of having it (Waymo on Greenhouse, live 2026-10-05)", () => {
    // A Canadian on a Mountain View internship got "No": the question was read
    // as "are you authorized?". A US citizen would have answered "Yes".
    const WAYMO = ["", "Yes", "No", "Unknown"];
    const label = "Do you require work authorization? (required) 9174883a";
    expect(value(ask(label, { options: WAYMO, kind: "choice", controlType: "select" }, P, US))).toBe("Yes");
    const citizen = { ...SPARSE_CANADIAN, location: "Austin, TX", workAuthorization: "U.S. citizen", authorizedUS: "Yes", requiresSponsorship: "No" };
    expect(value(ask(label, { options: WAYMO, kind: "choice", controlType: "select" }, citizen, US))).toBe("No");
    expect(value(ask("Will you need a work permit to work in Canada?", { options: YES_NO }, P))).toBe("No");
    expect(value(ask("Would you require work authorization to work in the United States?", { options: YES_NO }, P))).toBe("Yes");
    // A requirement stated, then the right itself asked: still the right.
    expect(value(ask("This position requires work authorization in the U.S. Are you authorized to work in the U.S.?", { options: YES_NO }, P))).toBe("No");
  });
});

describe("the question bank's work-authorization shapes (Greenhouse, 2026-10-05)", () => {
  const US = { jobCountry: "US" as string | null, company: "" };
  const citizen = { ...SPARSE_CANADIAN, location: "Denver, CO", workAuthorization: "US Citizen", authorizedUS: "Yes", requiresSponsorship: "No" };
  const opt = { ...SPARSE_CANADIAN, location: "Boston, MA", workAuthorization: "F-1 STEM OPT (EAD valid through June 2028)", authorizedUS: "Yes", requiresSponsorship: "Yes" };
  const berlin = { ...SPARSE_CANADIAN, location: "Berlin, Germany", workAuthorization: "German citizen (EU)", authorizedUS: "No", authorizedCanada: "No", requiresSponsorship: "Yes" };
  const canadian = { ...SPARSE_CANADIAN, requiresSponsorship: "No", authorizedUS: "No", authorizedCanada: "Yes" };

  it("authorized, authorized with a later need, or not: three statements (Datadog)", () => {
    // "Yes, but I will need sponsorship in the future" was written for an
    // applicant with no US work right at all.
    const q = "Are you legally authorised to work full-time in the country where this job is based?";
    const opts = ["Yes, no restriction.", "Yes, but I will need sponsorship in the future.", "No, I need sponsorship now."];
    const ask3 = (p: UserApplicationProfile) => value(ask(q, { options: opts, kind: "choice", controlType: "select" }, p, US));
    expect(ask3(berlin)).toBe("No, I need sponsorship now.");
    expect(ask3(canadian)).toBe("No, I need sponsorship now.");
    expect(ask3(opt)).toBe("Yes, but I will need sponsorship in the future.");
    expect(ask3(citizen)).toBe("Yes, no restriction.");
  });

  it("a label that says what Yes means is answered by it (Peloton)", () => {
    // Every applicant got the inverse: "Yes" confirmed no sponsorship needed.
    const q = "This position is not eligible for Visa Sponsorship. Applicants must be authorized to work in the United States without the need for Visa Sponsorship by the start date of employment. By selecting \"Yes,\" you confirm that you do not require Visa Sponsorship.";
    expect(value(ask(q, { options: YES_NO, kind: "boolean", controlType: "select" }, citizen, US))).toBe("Yes");
    expect(value(ask(q, { options: YES_NO, kind: "boolean", controlType: "select" }, opt, US))).toBe("No");
    expect(value(ask(q, { options: YES_NO, kind: "boolean", controlType: "select" }, berlin, US))).toBe("No");
    expect(value(ask(q, { options: YES_NO, kind: "boolean", controlType: "select" }, canadian, US))).toBe("No");
  });

  it("'authorized to lawfully work' is the work right (Roku, SoFi)", () => {
    // Unrecognized, it went to the AI, whose backend rule pass answers it Yes.
    const q = "Are you authorized to lawfully work in the country where this role is located?";
    expect(value(ask(q, { options: YES_NO }, berlin, US))).toBe("No");
    expect(value(ask(q, { options: YES_NO }, citizen, US))).toBe("Yes");
  });

  it("the type of support asked for is no yes or no (Pinterest)", () => {
    const q = "If you do require employee sponsorship or assistance for work authorization, please list the type of support you may require.";
    for (const p of [canadian, opt, citizen]) {
      const r = ask(q, { kind: "text" }, p, US);
      expect(String(value(r))).not.toMatch(/^(Yes|No)$/);
    }
  });
});

describe("question bank, status choices and acknowledgements (Greenhouse, 2026-10-05)", () => {
  const US = { jobCountry: "US" as string | null, company: "" };
  const CA = { jobCountry: "CA" as string | null, company: "" };
  const citizen = { ...SPARSE_CANADIAN, location: "Denver, CO", workAuthorization: "US Citizen", authorizedUS: "Yes", requiresSponsorship: "No" };
  const h1b = { ...SPARSE_CANADIAN, location: "Seattle, WA", workAuthorization: "H-1B visa (transfer required)", authorizedUS: "Yes", authorizedCanada: "No", requiresSponsorship: "Yes", willingToRelocate: "No" };
  const opt = { ...SPARSE_CANADIAN, location: "Boston, MA", workAuthorization: "F-1 STEM OPT (EAD valid through June 2028)", authorizedUS: "Yes", authorizedCanada: "No", requiresSponsorship: "Yes" };
  const berlin = { ...SPARSE_CANADIAN, location: "Berlin, Germany", workAuthorization: "German citizen (EU)", authorizedUS: "No", authorizedCanada: "No", requiresSponsorship: "Yes" };
  const canadian = { ...SPARSE_CANADIAN, requiresSponsorship: "No", authorizedUS: "No", authorizedCanada: "Yes", willingToRelocate: "Yes" };
  const choose = (label: string, options: string[], p: UserApplicationProfile, ctx = US) => value(ask(label, { options, kind: "choice", controlType: "select" }, p, ctx));

  it("export-control status: a citizen written 'US Citizen' is one; a visa holder is none of them (Astranis, SpaceX)", () => {
    const astranis = "Astranis complies with U.S. Government space technology export regulations, therefore will you state which of the following applies to you:";
    const opts = ["I am a U.S. Citizen.", "I am a lawful permanent resident of the U.S. and Green Card Holder.", "I am a refugee under 8 U.S.C. 1157.", "I am an asylee under 8 U.S.C. 1158.", "None of the above."];
    expect(choose(astranis, opts, citizen)).toBe("I am a U.S. Citizen.");
    expect(choose(astranis, opts, h1b)).toBe("None of the above.");
    expect(choose(astranis, opts, opt)).toBe("None of the above.");
    const spacex = ["(a) U.S. citizen or national of the United States", "(b) U.S. lawful permanent resident", "(c) Refugee under 8 U.S.C. 1157", "(d) Asylee under 8 U.S.C. 1158", "(e) Authorized to work in the United States under the Deferred Action for Childhood Arrivals (DACA) program", "(f) Other (please explain)"];
    expect(choose("Citizenship Status", spacex, citizen)).toBe("(a) U.S. citizen or national of the United States");
    expect(choose("Citizenship Status", spacex, h1b)).toBe("(f) Other (please explain)");
  });

  it("'I require … sponsorship' is a not-authorized statement, and a bare No answers too (Lyft, Coveo)", () => {
    const lyft = ["I am authorized to work for any employer in the country in which this position is based.", "I require/will require Lyft's sponsorship to obtain work authorization in the country in which this position is based (e.g. H-1B, TN, etc.)", "My status to work in the country in which this position is based is unknown."];
    expect(choose("Work Authorization", lyft, h1b, CA)).toBe(lyft[1]);
    const coveo = ["Yes, I am a Canadian citizen / permanent resident", "Yes, I have a valid study/work permit", "No"];
    expect(choose("Are you currently legally allowed to work in Canada for the duration of this internship?", coveo, h1b, CA)).toBe("No");
    expect(choose("Are you currently legally allowed to work in Canada for the duration of this internship?", coveo, canadian, CA)).toBe("Yes, I am a Canadian citizen / permanent resident");
  });

  it("sponsorship to remain where you live is about your own country (GitLab)", () => {
    const q = "Will you now or in the future require sponsorship for a visa to remain in your current location?";
    const opts = ["No", "Yes, Netherlands Highly Skilled Migrant Visa", "Yes, Ireland Highly Skilled Worker Visa", "Yes, EU Blue Card", "Yes, USMCA Professional (TN) Visa (USA)", "Yes, F-1 Visa OPT (USA)", "Yes, but not one of the visas listed here"];
    expect(choose(q, opts, canadian)).toBe("No");
    expect(choose(q, opts, berlin)).toBe("No");
  });

  it("a notice to acknowledge, its only option Yes, is acknowledged (Riot's E-Verify)", () => {
    const q = "Riot Games participates in E-Verify and will submit your information to the government for confirmation of your work authorization only after a conditional offer of employment has been made. By submitting an application, I acknowledge that I have read and understand the E-verify notice.";
    expect(choose(q, ["Yes"], citizen)).toBe("Yes");
  });

  it("'Does this commute work for you?' follows where the applicant lives and moves (Carvana)", () => {
    const q = "This position is located in Manville, NJ. Does this commute work for you?";
    expect(value(ask(q, { options: YES_NO }, h1b, US))).toBe("No");
    expect(value(ask(q, { options: YES_NO }, canadian, US))).toBe("Yes");
  });

  it("contract work for the company is employment history (Block)", () => {
    const q = "Have you ever provided any contract work for Block, Inc. or any of its subsidiaries or affiliates (whether in the U.S. or internationally)?*";
    expect(value(ask(q, { options: YES_NO }, canadian, CA))).toBe("No");
  });
});

describe("Greenhouse batch 2, live 2026-10-05: blanks a stated fact settles", () => {
  const berlin = { ...SPARSE_CANADIAN, location: "Berlin, Germany", workAuthorization: "German citizen (EU)", authorizedUS: "No", authorizedCanada: "No", requiresSponsorship: "Yes", willingToRelocate: "Yes", howDidYouHear: "Referral" };
  const seattle = { ...SPARSE_CANADIAN, location: "Seattle, WA", workAuthorization: "H-1B visa (transfer required)", authorizedUS: "Yes", requiresSponsorship: "Yes", willingToRelocate: "No" };
  const QC = { jobCountry: "CA" as string | null, company: "Coveo" };
  const CO = { jobCountry: "US" as string | null, company: "Anduril" };
  const YN = ["Please select", "Yes", "No"];

  it("Coveo: a referral among a referral and a friend; living there or about to move; a background check the candidate 'acknowledges'", () => {
    const where = ["Career Fair / Salon de l'emploi", "Coveo Employee Referral / Référence d'un employé de Coveo", "Friend or Former Colleague / Ami.e ou ancien.ne collègue", "Glassdoor", "LinkedIn"];
    expect(value(ask("Where did you hear about Coveo?", { options: where, kind: "choice", controlType: "select" }, berlin, QC))).toBe("Coveo Employee Referral / Référence d'un employé de Coveo");
    expect(value(ask("I confirm either living or being about to move to the location indicated in the job listing.*", { options: YN, kind: "boolean", controlType: "select" }, berlin, QC))).toBe("Yes");
    expect(value(ask("I confirm either living or being about to move to the location indicated in the job listing.*", { options: YN, kind: "boolean", controlType: "select" }, seattle, QC))).toBe("No");
    const check = "The candidate's employment is contingent upon the successful completion of a background check. The candidate acknowledges that any conditional employment offer may be withdrawn following an unsuccessful background check.";
    expect(value(ask(check, { options: YN, kind: "boolean", controlType: "select" }, berlin, QC))).toBe("Yes");
  });

  it("Anduril: 'If you are not local to Colorado, are you willing to relocate?' for a Seattle applicant who will not move; 'HISTORY WITH ANDURIL'", () => {
    expect(value(ask("If you are not local to Colorado, are you willing to relocate?*", { options: YES_NO }, seattle, CO))).toBe("No");
    expect(value(ask("Are you local to Colorado?", { options: YES_NO }, seattle, CO))).toBe("No");
    expect(value(ask("HISTORY WITH ANDURIL*", { options: YES_NO }, seattle, CO))).toBe("No");
  });
});

describe("Greenhouse batch 2, live 2026-10-05: Accenture Federal, Gemini, DoorDash, GitLab", () => {
  const US = { jobCountry: "US" as string | null, company: "Accenture Federal Services" };
  const citizen = { ...SPARSE_CANADIAN, location: "Denver, CO", workAuthorization: "US Citizen", authorizedUS: "Yes", requiresSponsorship: "No", willingToRelocate: "No", eeo: { veteranStatus: "I have never served in the military" }, experience: [{ company: "Ibotta", title: "Junior Software Engineer", startDate: "2022-08", endDate: "2026-06", description: "" }] };
  const h1b = { ...SPARSE_CANADIAN, location: "Seattle, WA", workAuthorization: "H-1B visa (transfer required)", authorizedUS: "Yes", requiresSponsorship: "Yes", willingToRelocate: "No" };
  const opt = { ...SPARSE_CANADIAN, location: "Boston, MA", workAuthorization: "F-1 STEM OPT (EAD valid through June 2028)", authorizedUS: "Yes", requiresSponsorship: "Yes" };

  it("AFS: citizenship status beside 'Not a US citizen…', government employment, the Reserves, a project at a current employer", () => {
    const status = ["US citizen", "Dual citizen (including US citizenship)", "Permanent resident / green card holder", "Refugee or Asylee", "Not a US citizen or permanent resident"];
    const q = "Many AFS positions require US citizenship. Please indicate your citizenship status so we can determine eligibility for specific roles.";
    expect(value(ask(q, { options: status, kind: "choice", controlType: "select" }, citizen as never, US))).toBe("US citizen");
    expect(value(ask(q, { options: status, kind: "choice", controlType: "select" }, h1b, US))).toBe("Not a US citizen or permanent resident");
    expect(value(ask("Are you a current employee of the U.S. Government (including U.S. Congress or military) or any state or local government?*", { options: YES_NO }, citizen as never, US))).toBe("No");
    expect(value(ask("Were you an employee of the U.S. Government (including U.S. Congress or military) or any state or local government within the past 10 years?*", { options: YES_NO }, citizen as never, US))).toBe("No");
    expect(value(ask("Will you be serving as enlisted personnel in either the Reserves or the National Guard while working for AFS?*", { options: YES_NO }, citizen as never, US))).toBe("No");
    expect(value(ask("At your current employer, are you currently working on a project with Accenture or have you worked on a project with Accenture in the past 24 months?*", { options: YES_NO }, citizen as never, US))).toBe("No");
  });

  it("Gemini: 'Are you open to relocating if you're not currently based there?' for a Seattle applicant who will not move", () => {
    const q = "This role is required to be based near our New York City, NY office. Are you open to relocating if you're not currently based there?*";
    expect(value(ask(q, { options: YES_NO }, h1b, { jobCountry: "US", company: "Gemini" }))).toBe("No");
  });

  it("DoorDash: sponsorship NOW is No for an OPT holder, Yes later; an H-1B needs it now", () => {
    const now = "Will you now require immigration sponsorship by our company to attain or maintain your employment authorization?*";
    const later = "Will you in the future require immigration sponsorship by our company to attain or maintain your employment authorization?*";
    expect(value(ask(now, { options: YES_NO }, opt, US))).toBe("No");
    expect(value(ask(later, { options: YES_NO }, opt, US))).toBe("Yes");
    expect(value(ask(now, { options: YES_NO }, h1b, US))).toBe("Yes");
  });

  it("DoorDash's whole label names STEM OPT as sponsorship: a STEM OPT holder needs it now, a plain OPT holder later", () => {
    // Live 2026-10-05, the label past the 78 characters a review shows. "STEM
    // OPT" here is an example of sponsorship, not a question about the OPT
    // extension (that rule answered both, and left a plain OPT holder blank).
    const eg = " to attain or maintain your employment eligibility (e.g., H-1B, E-3, TN, O-1, STEM OPT, or any immigration work authorization requiring a written submission from the company to a government agency)?*";
    const now = "Will you now require immigration sponsorship by our company" + eg;
    const later = "Will you in the future require immigration sponsorship by our company" + eg;
    const plain = { ...opt, workAuthorization: "F-1 OPT (EAD valid through June 2027)" };
    const stem = ask(now, { options: YES_NO }, opt, US);
    expect(value(stem)).toBe("Yes");
    expect(stem && stem.status === "answer" ? stem.rule : "").toMatch(/^sponsorship/);
    expect(value(ask(later, { options: YES_NO }, opt, US))).toBe("Yes");
    expect(value(ask(now, { options: YES_NO }, plain, US))).toBe("No");
    expect(value(ask(later, { options: YES_NO }, plain, US))).toBe("Yes");
    expect(value(ask(now, { options: YES_NO }, h1b, US))).toBe("Yes");
  });

  it("Toast: 'Do you now, or will you ever, require sponsorship' is the future too (an OPT holder said No)", () => {
    const q = "Do you now, or will you ever, require employment sponsorship to work in the country where this job is located?";
    expect(value(ask(q, { options: YES_NO }, opt, US))).toBe("Yes");
    expect(value(ask("Will you at any point require sponsorship to work in the US?", { options: YES_NO }, opt, US))).toBe("Yes");
  });

  it("Airbnb: 'Yes … now' / 'Yes … in the future' / 'No': when the need starts", () => {
    const q = "Will you now or in the future require company sponsorship to retain or extend your work authorization in the country where the job is located?";
    const opts = ["Yes, I will require immigration sponsorship now to legally work in the country where the job is located.", "Yes, I will require immigration sponsorship in the future to legally work in the country where the job is located.", "No, I do not and will not require immigration sponsorship to legally work in the country where the job is located."];
    const o = { options: opts, kind: "choice" as const, controlType: "select" as const };
    const plain = { ...opt, workAuthorization: "F-1 OPT (EAD valid through June 2027)" };
    expect(value(ask(q, o, h1b, US))).toBe(opts[0]);
    expect(value(ask(q, o, plain, US))).toBe(opts[1]);
    expect(value(ask(q, o, citizen as never, US))).toBe(opts[2]);
  });

  it("Duolingo: 'sponsored conferences' are no sponsorship question", () => {
    const q = "What Duolingo sponsored conferences have you attended and/or organizations are you a part of?";
    const r = ask(q, { options: ["Rewriting the Code", "ColorStack"], kind: "multiChoice" as never, controlType: "checkboxGroup" }, h1b, US);
    expect(r && r.status === "abstain" ? r.blockBackend : false).toBe(false);
    expect(r?.rule ?? "").not.toMatch(/^sponsorship/);
  });

  it("GitLab: 'Yes, <visa>' options: the applicant's own visa, else the Yes saying it is not listed", () => {
    const q = "Will you now or in the future require sponsorship for a visa to remain in your current location?*";
    const opts = ["No", "Yes, Netherlands Highly Skilled Migrant Visa", "Yes, Ireland Highly Skilled Worker Visa", "Yes, EU Blue Card", "Yes, USMCA Professional (TN) Visa (USA)", "Yes, F-1 Visa OPT (USA)", "Yes, but not one of the visas listed here"];
    const o = { options: opts, kind: "choice" as const, controlType: "combobox" as const };
    const ctx = { jobCountry: "US", company: "GitLab" };
    expect(value(ask(q, o, h1b, ctx))).toBe("Yes, but not one of the visas listed here");
    expect(value(ask(q, o, opt, ctx))).toBe("Yes, F-1 Visa OPT (USA)");
    expect(value(ask(q, o, citizen as never, ctx))).toBe("No");
    // A need with no visa stated: which Yes is unknown.
    expect(value(ask(q, o, { ...h1b, workAuthorization: "" }, ctx))).toBe("abstain");
  });

  it("GitLab: post-employment restrictions are the non-compete question", () => {
    expect(value(ask("Are you subject to any employment agreements and/or post-employment restrictions that could affect your ability to work at GitLab?*", { options: YES_NO }, h1b, US))).toBe("No");
  });
});

describe("conditional questions: the condition first", () => {
  const ACTIONET = ["Select an option...", "Yes", "No", "I am not a current or former government employee"];
  it("a false condition picks the option saying so (ActioNet on Jobvite, live 2026-10-03)", () => {
    expect(value(ask("If you are a current or former government employee, have you recused yourself in writing to the appropriate government official from working on all contracts or programs involving ActioNet?*", { options: ACTIONET, kind: "choice" }))).toBe("I am not a current or former government employee");
    expect(value(ask("If you are a current or former government employee, are you currently or were you ever previously involved in any ActioNet contracts or programs?*", { options: ACTIONET, kind: "choice" }))).toBe("I am not a current or former government employee");
  });
  it("…or stays blank, and away from the backend, when no option says so", () => {
    const r = ask("If you were referred, who referred you?", { kind: "text" });
    expect(value(r)).toBe("abstain");
    const yn = ask("If you are a current or former government employee, have you recused yourself?", { options: YES_NO });
    expect(value(yn)).toBe("abstain");
    expect(yn && yn.status === "abstain" && yn.blockBackend).toBe(true);
    // Brex (live 2026-10-03): never worked at Capital One, so no Employee ID.
    expect(value(ask("If you currently work, or have previously worked, at Capital One or a company acquired by Capital One, please provide your Employee ID (EID). This information is required for former/current employees.", { kind: "text" }))).toBe("abstain");
  });
  it("a condition that holds answers the question itself", () => {
    const p = { ...SPARSE_CANADIAN, gpa: "3.7/4.0" };
    expect(value(ask("If you are currently enrolled in or have graduated from a university, what is your GPA?", { kind: "text" }, p))).toBe("3.7/4.0");
  });
  it("a hypothetical holds; an unknown condition stays blank", () => {
    expect(value(ask("If you are offered this position, will you require visa sponsorship?", { options: YES_NO }, SPARSE_CANADIAN, { jobCountry: "CA", company: "" }))).toBe("No");
    expect(value(ask("If you hold an active security clearance, what level is it?", { kind: "text" }))).toBe("abstain");
  });
  it("'currently enrolled OR graduated' is true for either", () => {
    const grad = { ...SPARSE_CANADIAN, education: [{ school: "University of Toronto", degree: "BSc Computer Science", graduationYear: "2022" }] };
    expect(value(ask("Are you currently enrolled in or have graduated from a university?", { options: YES_NO }, grad))).toBe("Yes");
    expect(value(ask("Are you currently enrolled in or have graduated from a university?", { options: YES_NO }))).toBe("Yes");
  });
});

describe("citizenship", () => {
  it("answers citizenship of the stated country and abstains for others", () => {
    expect(value(ask("Are you a Canadian citizen?", { options: YES_NO }))).toBe("Yes");
    expect(value(ask("Are you a U.S. citizen?", { options: YES_NO }))).toBe("abstain");
  });

  it("a permanent resident is not a citizen, but is a 'citizen or permanent resident'", () => {
    const pr = { ...SPARSE_CANADIAN, workAuthorization: "Permanent resident of Canada" };
    expect(value(ask("Are you a citizen of Canada?", { options: YES_NO }, pr))).toBe("No");
    expect(value(ask("Are you a Canadian citizen or permanent resident?", { options: YES_NO }, pr))).toBe("Yes");
  });
});

describe("residence", () => {
  it("'Do you live in the United States?' → No for Toronto", () => {
    expect(value(ask("Do you live in the United States?", { options: ["YES", "NO"] }))).toBe("NO");
  });

  it("'Are you located in Ontario?' → Yes; 'in Quebec?' → No", () => {
    expect(value(ask("Are you currently located in Ontario?", { options: YES_NO }))).toBe("Yes");
    expect(value(ask("Are you currently located in Quebec?", { options: YES_NO }))).toBe("No");
  });

  it("a free-text yes/no residence question gets Yes/No, never a place name", () => {
    expect(value(ask("Are you currently located in Quebec?", { kind: "boolean" }))).toBe("No");
  });

  it("'live in or near Seattle? If not, willing to relocate?' depends on relocation, which is unknown", () => {
    expect(value(ask("Do you currently live in or near Seattle? If not, are you willing to relocate?", { options: ["YES", "NO"] }))).toBe("abstain");
    const willing = { ...SPARSE_CANADIAN, willingToRelocate: "Yes" };
    expect(value(ask("Do you currently live in or near Seattle? If not, are you willing to relocate?", { options: ["YES", "NO"] }, willing))).toBe("YES");
  });

  it("another city in the SAME country is a judgment call: abstain", () => {
    // Round 5: two well-known cities in different metros are apart (Toronto
    // for Ottawa, 450 km); a nearby one the tables do not place stays a call.
    expect(value(ask("Do you live in or near Ottawa?", { options: YES_NO }))).toBe("No");
    expect(value(ask("Do you live in or near Hamilton?", { options: YES_NO }))).toBe("abstain");
  });

  it("'Where are you located?' with continent options → North America", () => {
    expect(value(ask("Where are you located?", { options: ["New York Area", "North America", "South America", "Other"] }))).toBe("North America");
  });

  it("a preferred office question is not a residence question", () => {
    expect(ask("Preferred work location", { options: ["Toronto", "New York"] })).toBeNull();
  });

  it("a US-state select for a Canadian resident picks its explicit outside option", () => {
    const states = ["Select an option...", "Not Applicable", "Alabama", "Alaska", "California", "New York"];
    expect(value(ask("State", { options: states, category: "addressState", controlType: "select" }))).toBe("Not Applicable");
    expect(value(ask("State", { options: states.filter((s) => s !== "Not Applicable"), category: "addressState", controlType: "select" }))).toBe("abstain");
  });
});

describe("age", () => {
  const dob = { ...SPARSE_CANADIAN, dateOfBirth: "2003-06-02" };
  it("answers an age gate from the date of birth", () => {
    expect(value(ask("Are you 18 years of age or older?", { options: YES_NO }, dob))).toBe("Yes");
    expect(value(ask("Are you under the age of 18?", { options: YES_NO }, dob))).toBe("No");
  });
  it("without a date of birth, an adult (18+) gate defaults to Yes; a higher bar stays the applicant's", () => {
    expect(value(ask("Are you age 18 or older?", { options: ["Select an option...", "Yes", "No"] }))).toBe("Yes");
    expect(value(ask("Are you 21 years of age or older?", { options: YES_NO }))).toBe("abstain");
  });
  it("places an exact age in its bucket", () => {
    expect(value(ask("What is your age range?", { options: ["17 or younger", "18-20", "21-29", "30-39"] }, dob))).toBe("21-29");
  });
});

describe("experience", () => {
  it("total years into a bucket", () => {
    expect(value(ask("How many years of professional software development experience do you have?", { options: ["0-1 year", "1-3 years", "4-6 years", "Over 6 years"] }))).toBe("1-3 years");
  });
  it("whole years into a text field", () => {
    expect(value(ask("Please provide the total number of years of relevant experience you have", { kind: "number" }))).toBe("1");
  });
  it("a skill-specific question abstains", () => {
    expect(value(ask("How many years of experience do you have with Kubernetes?", { kind: "number" }))).toBe("abstain");
  });
  it("a domain the titles do not prove abstains", () => {
    expect(value(ask("How many years of sales experience do you have?", { kind: "number" }))).toBe("abstain");
  });
  it("'at least N years' compares", () => {
    expect(value(ask("Do you have at least 1 year of professional experience?", { options: YES_NO }))).toBe("Yes");
    expect(value(ask("Do you have 3+ years of professional experience?", { options: YES_NO }))).toBe("No");
  });
});

describe("education", () => {
  const levels = ["Select an option...", "High School", "GED", "Associates", "Bachelors", "Masters", "PhD"];
  it("highest education abstains while a higher degree is in progress", () => {
    expect(value(ask("What is your highest education?", { options: levels, controlType: "select" }))).toBe("abstain");
  });
  it("highest education of a graduate → their level", () => {
    const grad = { ...SPARSE_CANADIAN, education: [{ school: "uOttawa", degree: "BSc Computer Science", graduationYear: "2024" }] };
    expect(value(ask("What is your highest education?", { options: levels, controlType: "select" }, grad))).toBe("Bachelors");
  });
  it("'Do you have a bachelor's degree?' is No while still studying, Yes once pursuing counts", () => {
    expect(value(ask("Do you have a Bachelor's degree?", { options: YES_NO }))).toBe("No");
    expect(value(ask("Do you have or are you currently pursuing a Bachelor's degree?", { options: YES_NO }))).toBe("Yes");
  });
  it("school membership from the education list", () => {
    expect(value(ask("Are you currently attending or a recent graduate of the University of Florida?", { options: YES_NO }))).toBe("No");
    expect(value(ask("Are you a student at the University of Waterloo?", { options: YES_NO }))).toBe("Yes");
  });
  it("currently enrolled", () => {
    expect(value(ask("Are you currently a student?", { options: YES_NO }))).toBe("Yes");
  });
  it("school names by role", () => {
    expect(value(ask("What is the name of the school you are currently attending?", { kind: "longText", controlType: "textarea" }))).toBe("University of Waterloo");
    expect(value(ask("Where did you complete your undergraduate degree?", { kind: "text" }))).toBe("University of Waterloo");
  });
  it("graduation year", () => {
    expect(value(ask("Expected graduation year", { kind: "text" }))).toBe("2027");
    expect(value(ask("When is your expected graduation date?", { options: ["2025", "2026", "2027", "2028"], controlType: "select" }))).toBe("2027");
    expect(value(ask("When is your expected graduation date?", { options: ["May 2027", "December 2027"], controlType: "select" }))).toBe("abstain");
  });
});

describe("employment history", () => {
  it("'current or past employee of ActioNet?' → Neither", () => {
    expect(value(ask("Are you current or past employee of ActioNet, Inc.?", { options: ["Current ActioNet Employee", "Past ActioNet Employee", "Neither"], controlType: "select" }))).toBe("Neither");
  });
  it("'Have you ever worked for Shopify?' → Yes", () => {
    expect(value(ask("Have you ever worked for Shopify?", { options: YES_NO }))).toBe("Yes");
  });
  it("'worked for us' uses the job's company", () => {
    expect(value(ask("Have you previously worked for us?", { options: YES_NO }, SPARSE_CANADIAN, { jobCountry: null, company: "Kinaxis" }))).toBe("Yes");
  });
  it("a skill question is not an employer question", () => {
    expect(value(ask("Have you worked with React?", { options: YES_NO }))).not.toBe("No");
  });
  it("an employer the question does not name abstains", () => {
    expect(value(ask("Have you ever worked at a startup?", { options: YES_NO }))).toBe("abstain");
  });
  // Waymo's embedded Greenhouse form (live 2026-10-05; also in prod telemetry
  // 2026-09-28): "current OR former" before the name hid the company, and the
  // question went to the AI.
  const ALPHABET = "Are you a current or former Alphabet employee, intern, vendor, contractor, or temp (including Google and other Alphabet subsidiaries)? (required) dda2c2bb";
  const ALPHABET_OPTS = ["", "Current Alphabet Employee or Intern", "Former Alphabet Employee or Intern", "Current or Former member of Alphabet extended workforce", "Never worked at Alphabet"];
  it("'a current or former <Company> employee' names the company", () => {
    expect(value(ask(ALPHABET, { options: ALPHABET_OPTS, kind: "choice", controlType: "select" }))).toBe("Never worked at Alphabet");
  });
  it("every company the question names counts: a Google job is an Alphabet job here", () => {
    const googler = { ...SPARSE_CANADIAN, experience: [{ company: "Google", title: "Software Engineering Intern", startDate: "2024-05", endDate: "2024-08", description: "" }] };
    expect(value(ask(ALPHABET, { options: ALPHABET_OPTS, kind: "choice", controlType: "select" }, googler))).toBe("Former Alphabet Employee or Intern");
  });
  it("written, 'Have you ever worked for X? If yes, what was your position…' is No without X in the history (ConsumerAffairs on Workable, live 2026-10-05)", () => {
    const q = "*Have you ever worked for ConsumerAffairs? If yes, what was your position, and what dates were you employed?";
    expect(value(ask(q, { kind: "longText", controlType: "textarea" }, SPARSE_CANADIAN, { jobCountry: "US", company: "ConsumerAffairs" }))).toBe("No");
    // Someone who did work there describes it themselves.
    const q2 = "Have you ever worked for Shopify? If yes, what was your position, and what dates were you employed?";
    expect(value(ask(q2, { kind: "longText", controlType: "textarea" }))).not.toBe("No");
  });
  it("a bare 'current or former employee?' asks about the hiring company (Carvana, live 2026-10-05)", () => {
    const q = "Are you a current or former employee?*";
    expect(value(ask(q, { options: YES_NO }, SPARSE_CANADIAN, { jobCountry: null, company: "Carvana" }))).toBe("No");
    expect(value(ask(q, { options: YES_NO }, SPARSE_CANADIAN, { jobCountry: null, company: "Shopify" }))).toBe("Yes");
    // No hiring company known: the history cannot be checked.
    expect(value(ask(q, { options: YES_NO }))).not.toBe("No");
  });
});

describe("availability", () => {
  const notice = { ...SPARSE_CANADIAN, noticePeriod: "2 weeks" };
  it("start date from the notice period, formatted for the field", () => {
    expect(value(ask("Date Available", { kind: "date", placeholder: "mm/dd/yyyy" }, notice))).toBe("10/17/2026");
    expect(value(ask("When can you start?", { kind: "date", inputType: "date" }, notice))).toBe("2026-10-17");
  });
  it("'available to start within 4 weeks?' compares", () => {
    expect(value(ask("Are you available to start within 4 weeks?", { options: YES_NO }, notice))).toBe("Yes");
    expect(value(ask("Are you available to start within 1 week?", { options: YES_NO }, notice))).toBe("No");
  });
  it("a bare 'Start Date' is NOT an availability question (employment rows)", () => {
    expect(ask("Start Date", { kind: "date" }, notice)).toBeNull();
  });
});

describe("never answered from a profile", () => {
  it("opinions and a referrer's name stay unanswered; criminal history too", () => {
    expect(value(ask("Do you think AI will take over the world?", { options: ["Yes", "No", "Maybe So"] }))).toBe("abstain");
    expect(value(ask("Who referred you?"))).toBe("abstain");
    expect(value(ask("Have you ever been convicted of a felony?", { options: YES_NO }))).toBe("abstain");
  });
  it("channels, prior applications and background checks get the documented defaults (defaultAnswers.ts)", () => {
    expect(value(ask("How did you hear about us?", { options: ["LinkedIn", "Indeed"] }))).toBe("LinkedIn");
    expect(value(ask("How did you hear about us?", { options: ["LinkedIn", "Job Board", "Referral"] }))).toBe("Job Board");
    expect(value(ask("Do you anticipate having any challenges with clearing a background check?", { options: YES_NO }))).toBe("No");
    expect(value(ask("Are you willing to undergo a background check?", { options: YES_NO }))).toBe("Yes");
    expect(value(ask("Have you previously applied to Acme?", { options: YES_NO }))).toBe("No");
    // A free-text question a bare yes/no would not answer stays for the AI.
    expect(ask("Do you anticipate having any challenges with clearing a background check?", { kind: "longText" })).toBeNull();
  });
  it("conditional follow-ups", () => {
    expect(value(ask("If 'Other' selected for School Name, please indicate here"))).toBe("abstain");
    expect(value(ask("If yes, please explain"))).toBe("abstain");
  });
});

describe("stated facts", () => {
  it("relocation, licence, clearance, languages", () => {
    const p = { ...SPARSE_CANADIAN, willingToRelocate: "No", driversLicense: "Yes", securityClearance: "None", languages: "English (Native), French (Professional)" };
    expect(value(ask("Are you willing to relocate?", { options: YES_NO }, p))).toBe("No");
    expect(value(ask("Do you have a valid driver's license?", { options: YES_NO }, p))).toBe("Yes");
    expect(value(ask("Do you have an Active Clearance/Public Trust?", { options: ["Select an option...", "Yes", "No"] }, p))).toBe("No");
    expect(value(ask("Clearance Type", { options: ["Select an option...", "None", "Public Trust", "Secret"], controlType: "select" }, p))).toBe("None");
    expect(value(ask("Are you fluent in French?", { options: YES_NO }, p))).toBe("Yes");
    expect(value(ask("Do you speak Spanish?", { options: YES_NO }, p))).toBe("abstain");
  });
  it("relocation ASSISTANCE is not willingness, and is never defaulted", () => {
    const p = { ...SPARSE_CANADIAN, willingToRelocate: "Yes" };
    expect(value(ask("Will you require relocation assistance?", { options: YES_NO }, p))).not.toBe("Yes");
    expect(value(ask("Will you require relocation assistance?", { options: YES_NO }))).not.toBe("Yes");
  });
});

describe("helpers", () => {
  it("countryNamedIn: capital US only; 'North America' is not the US", () => {
    expect(countryNamedIn("Are you authorized to work in the US?")).toEqual({ code: "US" });
    expect(countryNamedIn("Can you work for us?")).toBeNull();
    expect(countryNamedIn("anywhere in North America")).toBeNull();
    expect(countryNamedIn("in this country")).toBe("this-country");
  });
  it("formatDateFor follows the placeholder", () => {
    const d = new Date(Date.UTC(2026, 9, 17));
    const base = { label: "", controlType: "text" as const, category: "unknown" as const, kind: "date" as const };
    expect(formatDateFor(d, { ...base, placeholder: "DD/MM/YYYY" })).toBe("17/10/2026");
    expect(formatDateFor(d, { ...base, placeholder: "YYYY-MM-DD" })).toBe("2026-10-17");
    expect(formatDateFor(d, { ...base })).toBe("10/17/2026");
  });
});

describe("residence: the applicant must be the subject", () => {
  // Lever, live 2026-10-03: the office is "located", not the applicant.
  it("an office-location commute question is not a residence question", () => {
    const label =
      "This position requires you to work from the Toronto Office located at 196 Spadina Avenue. Are you able to commute to the office 3 days a week?";
    const r = ask(label, { options: YES_NO });
    // Answered as an accepted REQUIREMENT of the posting, never as residence.
    expect(r && r.status === "answer" ? r.rule : "").toBe("default:accepts-requirement");
  });
  it("still answers 'Are you currently based in Toronto?'", () => {
    expect(value(ask("Are you currently based in Toronto?", { options: YES_NO }))).toBe("Yes");
  });
});

describe("'able to work' is a work-right question only with a country and no arrangement", () => {
  // Lever (Kepler), live 2026-10-03, verbatim.
  it("an office-attendance question is not work authorization", () => {
    const label =
      "This position requires you to work from the Toronto Office located at 24 Ward Street, Toronto ON M6H 4A6. Are you able to work from our Kepler office as required?";
    const r = ask(label, { options: YES_NO }, SPARSE_CANADIAN, { jobCountry: "CA", company: "Kepler" });
    // Office attendance is an accepted requirement, not work authorization.
    expect(r && r.status === "answer" ? r.rule : "").toBe("default:accepts-requirement");
  });
  it("'Are you able to work in Canada?' is", () => {
    expect(value(ask("Are you able to work in Canada?", { options: YES_NO }))).toBe("Yes");
  });
  it("'Are you legally able to work in Canada according to the laws…?' is", () => {
    expect(value(ask("Are you legally able to work in Canada according to the laws and regulations of the country?", { options: YES_NO }))).toBe("Yes");
  });
});

describe("which abstentions keep a field from the backend", () => {
  it("legal-status abstentions are blocked from the backend's rule pass", () => {
    const r = ask("Are you legally authorized to work in the United States?", { options: YES_NO });
    expect(r).toMatchObject({ status: "abstain", blockBackend: true });
  });
  it("opinions and essays stay available to the backend AI", () => {
    const r = ask("Do you think AI will take over the world?", { options: ["Yes", "No", "Maybe So"] });
    expect(r).toMatchObject({ status: "abstain", blockBackend: false });
  });
  it("a compound relocation-or-sponsorship question is not answered from one half", () => {
    const p = { ...SPARSE_CANADIAN, requiresSponsorship: "No" };
    expect(value(ask("Will you require relocation assistance or visa sponsorship?", { options: YES_NO }, p))).toBe("abstain");
  });
});

describe("high school questions are not the university's (Palantir on Lever, live 2026-10-03)", () => {
  // "High School Name" got "University of Waterloo" and "Year of High School
  // Graduation" got the university's 2027.
  it("abstains on the high school's name and graduation year", () => {
    expect(value(ask("High School Name", { controlType: "textarea", kind: "longText", category: "school" }))).toBe("abstain");
    expect(value(ask("Year of High School Graduation", { options: ["2020", "2021", "2022", "2023", "2027"], category: "school" }))).toBe("abstain");
    expect(value(ask("Which secondary school did you attend?", { category: "school" }))).toBe("abstain");
  });

  it("leaves the university and education-level questions to their own rules", () => {
    const rule = (r: ReturnType<typeof ask>) => (r && r.status === "abstain" ? r.rule : "");
    expect(rule(ask("Which university are you currently attending or did you last attend?", { category: "school" }))).not.toBe("high-school:not-in-profile");
    expect(rule(ask("What is the highest level of education you have completed?", { options: ["High School", "Bachelor's", "Master's"] }))).not.toBe("high-school:not-in-profile");
    expect(rule(ask("Do you have a high school diploma or GED?", { options: YES_NO }))).not.toBe("high-school:not-in-profile");
  });
});

describe("question bank 2026-10-05: education", () => {
  const P = () => import("./e2e/profiles.mjs") as Promise<Record<string, UserApplicationProfile>>;
  const US_JOB = { jobCountry: "US" as string | null, company: "Acme" };

  it("Samsara: 'enrolled in a 4-year Bachelor's program or have graduated within the past 2 years'", async () => {
    const { INDIA_NEW_GRAD, COMPLETE_CANADIAN, US_OPT_ANALYST, US_H1B_SENIOR, BERLIN_STAFF } = await P();
    const q = "Are you currently enrolled in an accredited 4-year Bachelor's degree program or have graduated within the past 2 years?";
    // A June 2026 B.Tech graduate said No: the graduation half was never read.
    expect(value(ask(q, { options: YES_NO }, INDIA_NEW_GRAD, US_JOB))).toBe("Yes");
    expect(value(ask(q, { options: YES_NO }, COMPLETE_CANADIAN, US_JOB))).toBe("Yes");
    expect(value(ask(q, { options: YES_NO }, US_OPT_ANALYST, US_JOB))).toBe("No");
    expect(value(ask(q, { options: YES_NO }, US_H1B_SENIOR, US_JOB))).toBe("No");
    // A German Diplom (no level we rank) finished long before the window.
    expect(value(ask(q, { options: YES_NO }, BERLIN_STAFF, US_JOB))).toBe("No");
  });

  it("an undergraduate GPA or degree result is the profile's GPA only when its degree is the bachelor's (Duolingo, Canonical)", async () => {
    const { COMPLETE_CANADIAN, US_OPT_ANALYST, INDIA_NEW_GRAD } = await P();
    // The OPT holder's 3.85 is her master's: written as her undergraduate GPA.
    expect(value(ask("Undergraduate GPA", { kind: "text" }, US_OPT_ANALYST, US_JOB))).toBe("abstain");
    expect(value(ask("Undergraduate GPA", { kind: "text" }, COMPLETE_CANADIAN, US_JOB))).toBe("3.7/4.0");
    expect(value(ask("Undergraduate GPA", { kind: "text" }, INDIA_NEW_GRAD, US_JOB))).toBe("8.6/10");
    // "…degree result…include the grading system" got "University of Waterloo, Bachelor of…".
    const result = "What was your bachelor's university degree result, or expected result if you have not yet graduated? Please include the grading system to help us understand your result.";
    expect(value(ask(result, { kind: "longText" }, COMPLETE_CANADIAN, US_JOB))).toBe("3.7/4.0");
    expect(value(ask(result, { kind: "longText" }, US_OPT_ANALYST, US_JOB))).toBe("abstain");
  });
});

describe("embargoed places, listed (question bank 2026-10-05: Databricks, Planet Labs)", () => {
  const P = () => import("./e2e/profiles.mjs") as Promise<Record<string, UserApplicationProfile>>;
  const US_JOB = { jobCountry: "US" as string | null, company: "Acme" };
  const DATABRICKS = ["Citizen or permanent resident of Cuba, Iran, North Korea, or Syria", "Ordinarily a resident of Cuba, Iran, North Korea, Syria or the Crimea, Donetsk, Luhansk, Zaporizhzhia, or Kherson regions of Ukraine", "Ordinarily a resident of Russia or Belarus and not willing to relocate for a Databricks role", "None of the above"];
  const LIST = "Please confirm whether any of the below applies to you. Select all that apply. Note: This information will only be used to ensure compliance with U.S. sanctions and export controls.";

  it("a citizen and resident of a country no option names: None of the above", async () => {
    const { INDIA_NEW_GRAD, BERLIN_STAFF } = await P();
    const o = { options: DATABRICKS, kind: "multiChoice" as never, controlType: "checkboxGroup" as const };
    expect(value(ask(LIST, o, INDIA_NEW_GRAD, US_JOB))).toBe("None of the above");
    expect(value(ask(LIST, o, BERLIN_STAFF, US_JOB))).toBe("None of the above");
  });

  it("…but never without the citizenship, nor when an option names the applicant's country", async () => {
    const { US_H1B_SENIOR, INDIA_NEW_GRAD } = await P();
    const o = { options: DATABRICKS, kind: "multiChoice" as never, controlType: "checkboxGroup" as const };
    expect(value(ask(LIST, o, US_H1B_SENIOR, US_JOB))).toBe("abstain");
    const kyiv = { ...INDIA_NEW_GRAD, location: "Kyiv, Ukraine", country: "Ukraine", workAuthorization: "Ukrainian citizen" };
    expect(value(ask(LIST, o, kyiv as never, US_JOB))).not.toBe("None of the above");
  });

  it("'Do you reside in … any of the following countries: Cuba, Iran, North Korea, Syria…?' checks every one", async () => {
    const { INDIA_NEW_GRAD } = await P();
    const q = "Do you reside in or maintain an established permanent residence in any of the following countries: Cuba, Iran, North Korea, Syria, or the following territories: Crimea, Donetsk, Luhansk?";
    expect(value(ask(q, { options: YES_NO }, INDIA_NEW_GRAD, US_JOB))).toBe("No");
  });
});

describe("graduation options with open ends (question bank 2026-10-05)", () => {
  const P = () => import("./e2e/profiles.mjs") as Promise<Record<string, UserApplicationProfile>>;
  const US_JOB = { jobCountry: "US" as string | null, company: "Acme" };
  it("'Earlier than Fall 2026' for a June 2026 graduate and a 2014 one; 'Already graduated' for a graduate (Databricks, Riot)", async () => {
    const { INDIA_NEW_GRAD, US_H1B_SENIOR, COMPLETE_CANADIAN } = await P();
    const databricks = { options: ["Earlier than Fall 2026", "Fall 2026", "Spring 2027", "Later than Summer 2027"], kind: "choice" as const, controlType: "select" as const };
    expect(value(ask("What is your graduation date?", databricks, INDIA_NEW_GRAD, US_JOB))).toBe("Earlier than Fall 2026");
    expect(value(ask("What is your graduation date?", databricks, US_H1B_SENIOR, US_JOB))).toBe("Earlier than Fall 2026");
    expect(value(ask("What is your graduation date?", databricks, COMPLETE_CANADIAN, US_JOB))).toBe("Spring 2027");
    const riot = { options: ["2028", "2027", "Already graduated"], kind: "choice" as const, controlType: "select" as const };
    const q = "Please select the year you anticipate graduating from your academic program.";
    expect(value(ask(q, riot, US_H1B_SENIOR, US_JOB))).toBe("Already graduated");
    expect(value(ask(q, riot, COMPLETE_CANADIAN, US_JOB))).toBe("2027");
  });
});

describe("years of a narrower experience (question bank 2026-10-05: AlayaCare)", () => {
  const P = () => import("./e2e/profiles.mjs") as Promise<Record<string, UserApplicationProfile>>;
  it("'professional fullstack … experience (Python/React or Vuejs)' is no career total", async () => {
    const { BERLIN_STAFF } = await P();
    const o = { options: ["1-2 years", "3 years", "3–4 years", "5+ years"], kind: "choice" as const, controlType: "select" as const };
    expect(value(ask("How many years of professional fullstack software engineering experience do you have (Python/React or Vuejs)?", o, BERLIN_STAFF))).toBe("abstain");
    expect(value(ask("How many years of professional fullstack software engineering experience do you have?", o, BERLIN_STAFF))).toBe("abstain");
    // The plain software-engineering total still answers.
    expect(value(ask("How many years of professional software engineering experience do you have?", o, BERLIN_STAFF))).toBe("5+ years");
  });
});

describe("U.S. person status worded with a 'not' (Hermeus on Lever, regression 2026-10-05)", () => {
  it("'…refugees with such status granted, not pending' is still the U.S.-person option", () => {
    const gc = { ...SPARSE_CANADIAN, location: "San Jose, CA", country: "USA", workAuthorization: "U.S. permanent resident (green card)", authorizedUS: "Yes", requiresSponsorship: "No" };
    const opts = ["U.S. person. This status includes U.S. citizens, U.S. nationals, lawful permanent residents (green card holders), and asylums and refugees with such status granted, not pending.", "Foreign person. This ITAR/EAR status includes anyone who is not a U.S. person (see above)."];
    expect(value(ask("U.S. EXPORT COMPLIANCE", { options: opts, kind: "choice", controlType: "select" }, gc as never, { jobCountry: "US", company: "Hermeus" }))).toBe(opts[0]);
  });
});

describe("question bank 2 (2026-10-05): Anduril, Accenture Federal, Baselayer", () => {
  const P = () => import("./e2e/profiles.mjs") as Promise<Record<string, UserApplicationProfile>>;
  const US_JOB = { jobCountry: "US" as string | null, company: "Acme" };
  it("export controls: 'A United States citizen or national' is a US citizen's (Anduril)", async () => {
    const { BOOTCAMP_CAREER_GAP } = await P();
    const opts = ["A United States citizen or national", "A person lawfully admitted for permanent residence of the United States (i.e., “Green Card” holder)", "A person admitted as a refugee to the United States under 8 U.S.C. 1157", "A person admitted as an asylee to the United States under 8 U.S.C. 1158", "None of the above"];
    const q = "EXPORT CONTROLS - This position requires access to information and technology that is subject to U.S. export controls. Your responses to the questions below will be used to determine eligibility.";
    expect(value(ask(q, { options: opts, kind: "choice", controlType: "select" }, BOOTCAMP_CAREER_GAP, US_JOB))).toBe(opts[0]);
  });
  it("'If you are not local to Colorado, are you willing to relocate?' is Yes from Berlin for someone who will move", async () => {
    const { BERLIN_STAFF, INDIA_NEW_GRAD } = await P();
    const q = "If you are not local to Colorado, are you willing to relocate?";
    expect(value(ask(q, { options: YES_NO }, BERLIN_STAFF, US_JOB))).toBe("Yes");
    expect(value(ask(q, { options: YES_NO }, INDIA_NEW_GRAD, US_JOB))).toBe("Yes");
  });
  it("a 'Select...' placeholder is no option: 'willing to relocate? Yes | No | Not Applicable' (Baselayer)", async () => {
    const { BERLIN_STAFF } = await P();
    expect(value(ask("Are you willing to relocate?", { options: ["Select...", "Yes", "No", "Not Applicable"], kind: "choice", controlType: "select" }, BERLIN_STAFF, US_JOB))).toBe("Yes");
  });
});
