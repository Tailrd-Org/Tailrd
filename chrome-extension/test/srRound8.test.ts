// @vitest-environment-options {"url": "https://jobs.smartrecruiters.com/oneclick-ui/company/qbank/publication/00000000-0000-0000-0000-000000000000"}
/**
 * Round 8's SmartRecruiters postings taken to their screening step live
 * (2026-10-21: ServiceNow, Intuitive). Each test is a blank a stated fact
 * answers, or an answer read wrong, on the real step; labels and options
 * verbatim from each posting's one-click page (GET only).
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { scanPage } from "../src/content/formScanner";
import { fillAriaCombobox, readComboboxOptions } from "../src/content/comboboxEngine";
import { setResolveContext } from "../src/content/fieldResolver";
import { detectJobPlace } from "../src/content/jobLocation";
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

const persona = (who: string | UserApplicationProfile): UserApplicationProfile =>
  typeof who === "string" ? ((P as Record<string, unknown>)[who] as UserApplicationProfile) : who;
const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const YN = ["Yes", "No"];

/** One question on a page placed in `location`: each persona's proposed answer (null when blank). */
function scanned(who: Record<string, string | UserApplicationProfile>, label: string, options: string[], location = "Santa Clara, CA, United States"): Record<string, string | null> {
  const control = options.length
    ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>`
    : `<input type="text" id="q0" name="q0">`;
  const out: Record<string, string | null> = {};
  for (const [name, p] of Object.entries(who)) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form"><div class="field"><label for="q0">${esc(label)}</label>${control}</div><div class="field"><label for="em">Email</label><input type="text" id="em" name="email"></div></form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: "ServiceNow" });
    const f = scanPage(persona(p), true).fields.find((x) => x.label.startsWith(label.slice(0, 40)));
    out[name] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}
const as = (...names: string[]): Record<string, string> => Object.fromEntries(names.map((n) => [n, n]));

describe("returning to school after the internship (ServiceNow)", () => {
  it("'Following the completion of your internship…' is the same question as 'after': No for a graduate", () => {
    const got = scanned(as("NEW_GRAD_DECLINER", "US_OPT_ANALYST", "COMPLETE_CANADIAN"), "Following the completion of your internship, will you be returning to school to complete at least one semester/quarter? *", YN);
    // A student's own plans stay theirs (the standing rule).
    expect(got).toEqual({ NEW_GRAD_DECLINER: "No", US_OPT_ANALYST: "No", COMPLETE_CANADIAN: null });
  });
});

describe("a question for residents of listed states (Intuitive)", () => {
  const label =
    "Preference will be given to qualified candidates who do not reside, or plan to reside, in Alabama, Arkansas, Delaware, Florida, Indiana, Iowa, Louisiana, Maryland, Mississippi, Missouri, Oklahoma, Pennsylvania, South Carolina, or Tennessee.If you live in one of these states, would you consider relocation? *";
  const opts = ["Yes", "No", "N/A, I do not live in one of these states"];
  it("its N/A option for someone who lives in none of them", () => {
    const got = scanned(as("US_H1B_SENIOR", "US_VETERAN", "COMPLETE_CANADIAN"), label, opts, "Sunnyvale, CA, United States");
    expect(got).toEqual({ US_H1B_SENIOR: opts[2], US_VETERAN: opts[2], COMPLETE_CANADIAN: opts[2] });
  });
  it("the relocation answer for someone who lives in one", () => {
    const base = persona("US_VETERAN");
    const pittsburgh = { ...base, location: "Pittsburgh, PA", addressCity: "Pittsburgh", addressState: "PA", postalCode: "15213", willingToRelocate: "Yes" } as UserApplicationProfile;
    const staying = { ...pittsburgh, willingToRelocate: "No" } as UserApplicationProfile;
    const got = scanned({ pittsburgh, staying }, label, opts, "Sunnyvale, CA, United States");
    expect(got).toEqual({ pittsburgh: "Yes", staying: "No" });
  });
});

describe("ServiceNow's education lists ('If unlisted, please select \"Other\"')", () => {
  const SCHOOLS = ["Alabama A&M University", "Arizona State University", "Benedict College", "Bentley University", "Boston College", "Boston University", "Brigham Young University", "California Polytechnic State University", "Carnegie Mellon University", "Columbia University", "Cornell University", "CUNY Bernard M Baruch College", "Duke University", "Fisk University", "Florida International University", "George Washington University", "Georgia Institute of Technology", "Georgia State University", "Harvey Mudd College", "Indiana University Bloomington", "Kansas State University", "Loyola Marymount University", "Massachusetts Institute of Technology", "Michigan State University", "Mila - Quebec AI Institute", "New York University", "North Carolina A&T University", "Northeastern University", "Northwestern University", "Ohio University", "Other", "Parsons School of Design", "Philander Smith University", "Pomona College", "Prairie View A&M University", "Princeton University", "Providence College", "Purdue University", "Rochester Institute of Technology", "Rutgers University – New Brunswick", "San Diego State University", "San Jose State University", "Santa Clara University", "Southern University and A&M College", "Stanford University", "Texas A&M University", "The University of Texas at Austin", "The University of Texas at Dallas", "The University of Texas at El Paso", "University of California, Berkeley", "University of California, Irvine", "University of California, Los Angeles", "University of California, Santa Barbara", "University of California, San Diego", "University of Central Florida", "University of Chicago", "University of Colorado, Boulder", "University of Florida", "University of Georgia", "University of Illinois Urbana-Champaign", "University of Maryland, College Park", "University of Massachusetts Amherst", "University of Miami", "University of Michigan", "University of North Carolina at Chapel Hill", "University of Pennsylvania", "University of Puerto Rico", "University of Rochester", "University of Southern California", "University of Washington"];
  const MAJORS = ["Accounting", "Aerospace, Aeronautical, and Astronautical/Space Engineering", "Artificial Intelligence/Machine Learning", "Biochemistry", "Bioengineering and Biomedical Engineering", "Biology/Biological Sciences", "Business Administration and Management", "Chemical Engineering", "Chemistry", "Cinematography and Film/Video Production", "Civil Engineering", "Communications", "Computer and Information Sciences", "Computer Engineering", "Computer Science", "Criminal Justice/Criminology", "Data Analytics", "Economics", "Electrical/Electronics Engineering", "Engineering", "Environmental Science", "Finance", "Fine/Studio Arts", "Graphic Design", "Health/Health Care Administration/Management", "History", "Human Resources", "Industrial Engineering", "Information Technology", "International Business/Relations/Affairs", "Journalism", "Liberal Arts and Sciences/Liberal Studies", "Logistics, Materials, and Supply Chain Management", "Management Information Systems", "Management Science", "Marketing", "Mathematics", "Mechanical Engineering", "Other", "Philosophy", "Physics", "Political Science and Government", "Psychology", "Public Relations", "Sales", "Solution Consulting"];
  it("a school the list does not carry is its 'Other' (decision 95)", () => {
    const got = scanned(as("UK_SENIOR", "NEW_GRAD_DECLINER", "COMPLETE_CANADIAN", "INDIA_NEW_GRAD", "US_H1B_SENIOR", "US_GREENCARD_STUDENT", "US_F1_STUDENT"), 'What school do you currently attend or most recently attended? If unlisted, please select "Other" *', SCHOOLS);
    expect(got).toEqual({
      UK_SENIOR: "Other",
      NEW_GRAD_DECLINER: "Other",
      COMPLETE_CANADIAN: "Other",
      INDIA_NEW_GRAD: "Other",
      // Past the panel's first 60: still read.
      US_H1B_SENIOR: "University of Washington",
      US_GREENCARD_STUDENT: "San Jose State University",
      US_F1_STUDENT: "Massachusetts Institute of Technology",
    });
  });
  it("the major's own option over the broad one", () => {
    const got = scanned(as("NEW_GRAD_DECLINER", "UK_SENIOR", "BERLIN_STAFF", "US_F1_STUDENT", "INDIA_NEW_GRAD"), 'What is your major? If unlisted, please select "Other" *', MAJORS);
    expect(got).toEqual({
      NEW_GRAD_DECLINER: "Electrical/Electronics Engineering",
      // Computing (Imperial's MSc) and Informatik (a German Diplom) are computer science.
      UK_SENIOR: "Computer Science",
      BERLIN_STAFF: "Computer Science",
      US_F1_STUDENT: "Computer Science",
      INDIA_NEW_GRAD: "Computer Science",
    });
  });
  it("a graduation year before the listed years is 'Other'", () => {
    const got = scanned(as("UK_SENIOR", "US_H1B_SENIOR", "NEW_GRAD_DECLINER"), "Expected Graduation Date? *", ["2025", "2026", "2027", "2028", "2029", "Other"]);
    expect(got).toEqual({ UK_SENIOR: "Other", US_H1B_SENIOR: "Other", NEW_GRAD_DECLINER: "2026" });
  });
});

/**
 * SmartRecruiters' dropdown items, as served (round 8): each <spl-dropdown-item>
 * draws its role=option in a shadow root, its text arrives through two slots,
 * and the text sits in an <spl-truncate> whose <spl-tooltip> repeats it in a
 * role=tooltip box. Read through the tooltip, every option was its label twice
 * ("WA WA"), and Intuitive's "What is your State or Province?" stayed blank
 * for a Seattle and an Austin applicant (live).
 */
function splDropdownItem(text: string): HTMLElement {
  const item = document.createElement("spl-dropdown-item");
  const isr = item.attachShadow({ mode: "open" });
  const opt = document.createElement("div");
  opt.setAttribute("role", "option");
  opt.setAttribute("aria-selected", "false");
  const prefix = document.createElement("slot");
  prefix.name = "prefix";
  const body = document.createElement("spl-typography-body");
  body.attachShadow({ mode: "open" }).innerHTML = "<p><slot></slot></p>";
  body.append(document.createElement("slot"));
  opt.append(prefix, body);
  isr.append(opt);
  const truncate = document.createElement("spl-truncate");
  truncate.setAttribute("title", text);
  const tooltip = document.createElement("spl-tooltip");
  tooltip.setAttribute("content", text);
  tooltip.attachShadow({ mode: "open" }).innerHTML = `<span class="c-spl-tooltip"><span class="c-spl-tooltip-slot"><slot></slot></span><div role="tooltip" class="c-spl-tooltip-wrapper"><div class="c-spl-tooltip-wrapper-content">${esc(text)}</div></div></span>`;
  tooltip.innerHTML = '<div class="c-spl-truncate"><slot></slot></div>';
  truncate.attachShadow({ mode: "open" }).append(tooltip);
  truncate.textContent = text;
  item.append(truncate);
  return item;
}

function srCombobox(label: string, options: string[]): HTMLInputElement {
  const input = document.createElement("input");
  input.type = "text";
  input.id = "question_q0";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-controls", "menu-question_q0");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  const menu = document.createElement("div");
  menu.id = "menu-question_q0";
  menu.setAttribute("role", "listbox");
  for (const o of options) menu.append(splDropdownItem(o));
  const lab = document.createElement("label");
  lab.htmlFor = "question_q0";
  lab.textContent = label;
  const field = document.createElement("div");
  field.className = "field";
  field.append(lab, input, menu);
  document.getElementById("application-form")!.append(field);
  return input;
}

describe("SmartRecruiters' dropdown items read once, not through their tooltip", () => {
  const STATES = ["AK", "AL", "AZ", "CA", "CO", "MA", "NY", "TX", "WA"];
  it("each option's text is its label", () => {
    document.body.innerHTML = `<form id="application-form"></form>`;
    const input = srCombobox("What is your State or Province? *", ["Yes", "No", "N/A, I do not live in one of these states"]);
    expect(readComboboxOptions(input, Infinity)).toEqual(["Yes", "No", "N/A, I do not live in one of these states"]);
  });
  it("so the state is answered on the scan path", () => {
    const got: Record<string, string | null> = {};
    for (const who of ["US_H1B_SENIOR", "US_VETERAN"]) {
      document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">Sunnyvale, CA, United States</div><form id="application-form"></form>`;
      srCombobox("What is your State or Province? *", STATES);
      const f = scanPage(persona(who), true).fields.find((x) => x.label.startsWith("What is your State"));
      got[who] = f?.proposedValue ?? null;
    }
    expect(got).toEqual({ US_H1B_SENIOR: "WA", US_VETERAN: "TX" });
  });
});

describe("Western Digital's and Intuitive's screening steps (live, round 8)", () => {
  it("a family or close friend at the company: the relatives default", () => {
    const got = scanned(as("US_GREENCARD_STUDENT", "BERLIN_STAFF"), "Do you have a Family or Close Friend relationship with anyone employed by WD? *", YN, "Rochester, MN, United States");
    expect(got).toEqual({ US_GREENCARD_STUDENT: "No", BERLIN_STAFF: "No" });
  });
  it("the posting's own work period is accepted by applying (decision 49)", () => {
    const got = scanned(as("INDIA_NEW_GRAD", "COMPLETE_CANADIAN"), "Are you available to work during the work period specified in the eligibility section of the job posting? *", YN, "Sunnyvale, CA, United States");
    expect(got).toEqual({ INDIA_NEW_GRAD: "Yes", COMPLETE_CANADIAN: "Yes" });
  });
  it("a follow-up on the previous selection is not asked of the one it does not concern", () => {
    // The question before it took the applicant's own country; "India" was
    // written again here (Intuitive, live).
    const got = scanned(as("INDIA_NEW_GRAD", "COMPLETE_CANADIAN"), "If your selection is not your most recent Citizenship or Permanent Residence, please indicate your most recent Citizenship or Permanent Residence below:", [], "Sunnyvale, CA, United States");
    expect(got).toEqual({ INDIA_NEW_GRAD: null, COMPLETE_CANADIAN: null });
  });
});

/**
 * SmartRecruiters' page 1 as served (round 8): "Apply with resume" (a box that
 * parses a résumé to fill the form) at the top, and the application's own
 * "Resume" section below it. Each upload is an <spl-dropzone> whose file input
 * and "Choose a file / or drop it here" label live in its shadow root; the
 * section's "Resume" title and the dropzone's data-test="resume-upload" are
 * outside it. The scan never saw a résumé field, so no résumé was ever
 * attached on SmartRecruiters, where most postings require one.
 */
function splDropzone(testId: string, n: number): HTMLElement {
  const dz = document.createElement("spl-dropzone");
  dz.setAttribute("data-test", testId);
  dz.setAttribute("label", "");
  dz.attachShadow({ mode: "open" }).innerHTML =
    `<div class="c-spl-dropzone-wrapper c-spl-dropzone"><slot name="icon"></slot>` +
    `<label for="spl-dropzone-file-input-${n}"><span class="c-spl-dropzone-label c-spl-dropzone-label-browse">Choose a file</span><span class="c-spl-dropzone-label">or drop it here</span></label>` +
    `<input type="file" id="spl-dropzone-file-input-${n}" accept=".doc, .pdf" aria-describedby="spl-dropzone-restriction-label-${n}">` +
    `<span class="c-spl-dropzone-label c-spl-dropzone-label-restrict" id="spl-dropzone-restriction-label-${n}">10MB size limit</span></div><ul class="c-spl-file-list"></ul>`;
  const field = document.createElement("spl-form-field");
  field.append(dz);
  return field;
}

describe("SmartRecruiters' résumé upload is found across its shadow root, never the parse box", () => {
  it("the Resume section's dropzone is the résumé field; Apply with resume is not", () => {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div id="app"><form></form></div>`;
    const form = document.querySelector("form")!;
    const apply = document.createElement("oc-apply-with-resume");
    const applyBox = document.createElement("div");
    applyBox.className = "spl-flex spl-flex-col";
    applyBox.append(splDropzone("apply-with-resume", 2));
    apply.append(applyBox);
    const top = document.createElement("div");
    top.className = "form-section";
    top.append(apply);
    const resume = document.createElement("oc-resume-upload");
    resume.innerHTML = `<div data-test="resume-upload-container" class="form-section"><spl-typography-title variant="lg" data-test="section-title">Resume</spl-typography-title></div>`;
    resume.firstElementChild!.append(splDropzone("resume-upload", 1));
    const name = document.createElement("div");
    name.innerHTML = `<label for="fn">First name</label><input id="fn" type="text">`;
    form.append(top, name, resume);
    const { fields } = scanPage(persona("US_F1_STUDENT"), true);
    const resumes = fields.filter((f) => f.category === "resumeUpload");
    expect(resumes.length).toBe(1);
    const input = resume.querySelector("spl-dropzone")!.shadowRoot!.querySelector("input")!;
    expect(resumes[0].id).toBe(input.getAttribute("data-ap-field"));
  });
});

/**
 * SmartRecruiters' multi-select (Western Digital's "What is your major?", live,
 * round 8): a pick becomes an <spl-tag label="…"> inside <spl-tags-list>'s
 * own shadow root and the box empties. The engine looked for the pick as text
 * beside the box, found none, and reported "Selection didn't stick" for a
 * major the page held.
 */
function splMultiselect(options: string[]): HTMLInputElement {
  const host = document.createElement("spl-multiselect-autocomplete");
  host.id = "question_major";
  const sr = host.attachShadow({ mode: "open" });
  const trigger = document.createElement("div");
  trigger.className = "c-spl-multiselect-autocomplete-trigger";
  const tags = document.createElement("spl-tags-list");
  tags.className = "c-spl-multiselect-autocomplete-tags";
  const tagsRoot = tags.attachShadow({ mode: "open" });
  tagsRoot.innerHTML = `<div class="c-spl-tags-list"><div class="c-spl-tags-list-items" role="listbox" aria-label="Tags list"></div></div>`;
  const wrap = document.createElement("div");
  wrap.className = "c-spl-multiselect-autocomplete-input-wrapper";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "c-spl-multiselect-autocomplete-input";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", "menu-question_major");
  input.setAttribute("aria-expanded", "false");
  wrap.append(input);
  trigger.append(tags, wrap);
  const menu = document.createElement("div");
  menu.id = "menu-question_major";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  const render = (): void => {
    menu.innerHTML = "";
    for (const o of options.filter((x) => x.toLowerCase().includes(input.value.toLowerCase()))) {
      const item = splDropdownItem(o);
      item.shadowRoot!.querySelector('[role="option"]')!.addEventListener("click", () => {
        const holder = document.createElement("div");
        holder.setAttribute("role", "presentation");
        holder.className = "c-spl-tags-list-item c-spl-tags-list-item--tag";
        const tag = document.createElement("spl-tag");
        tag.setAttribute("label", o);
        tag.attachShadow({ mode: "open" }).innerHTML = `<span class="c-spl-tag"><span class="c-spl-tag-label"><slot></slot></span></span>`;
        holder.append(tag);
        tagsRoot.querySelector(".c-spl-tags-list-items")!.append(holder);
        input.value = "";
        menu.hidden = true;
        input.setAttribute("aria-expanded", "false");
      });
      menu.append(item);
    }
  };
  const openMenu = (): void => {
    render();
    menu.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };
  input.addEventListener("click", openMenu);
  input.addEventListener("input", openMenu);
  sr.append(trigger, menu);
  document.body.append(host);
  return input;
}

describe("a SmartRecruiters multi-select's pick is its tag (Western Digital's major)", () => {
  it("a pick shown as an spl-tag sticks", async () => {
    document.body.innerHTML = "";
    const input = splMultiselect(["Business Administration", "Accounting", "Computer Science/Software Engineering", "Computer Engineering", "Other"]);
    const r = await fillAriaCombobox(input, "Computer Science/Software Engineering", { sleep: async () => {}, openWaitMs: 100, commitWaitMs: 100, pollMs: 5 });
    expect(r.reason ?? "").toBe("");
    expect(r.filled).toBe(true);
  });
});
