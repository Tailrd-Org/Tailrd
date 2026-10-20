import { describe, it, expect, beforeEach, beforeAll, afterAll } from "vitest";
import { findAdvanceButton, findPendingAdvance } from "../src/content/advance";
import { LOGIN_ADVANCE_RE, SIGNUP_ADVANCE_RE } from "../src/content/accountFlow";
import type { SiteAdapter } from "../src/content/adapters/types";
import { stubLayout } from "./helpers/layout";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());

beforeEach(() => {
  document.body.innerHTML = "";
});

function scopeWith(html: string): HTMLElement {
  document.body.innerHTML = `<div id="scope">${html}</div>`;
  return document.getElementById("scope")!;
}

describe("SmartRecruiters' one-click page (live, round 7)", () => {
  // Its step button is a web component, and beside the form sits the
  // third-party "Apply With Indeed": the flow took that for the form's own
  // submit and said "Review and submit" on page 1, before the screening
  // questions. Markup from the page (Western Digital).
  const PAGE =
    `<oc-external-apply-button><button type="button" class="external-apply-button external-apply-button--indeed">Apply With Indeed</button></oc-external-apply-button>` +
    `<footer data-test="footer" class="form-section form-section--clean"><div class="spl-flex flex-row spl-justify-between"><div></div><div><oc-button type="primary" data-test="footer-next"><spl-button data-test="" aria-label="" type="primary" stretch="no"><span>Next</span></spl-button></oc-button></div></div></footer>`;
  it("Next is the step's advance; another site's Apply is never the form's submit", () => {
    const hit = findAdvanceButton(scopeWith(PAGE), null);
    expect(hit?.kind).toBe("advance");
    expect(hit?.el.textContent?.trim()).toBe("Next");
  });
  it("apply-with buttons alone end nothing", () => {
    for (const label of ["Apply With Indeed", "Apply with LinkedIn", "Apply using SEEK"]) {
      expect(findAdvanceButton(scopeWith(`<button class="x">${label}</button>`), null), label).toBeNull();
    }
  });
});

describe("findAdvanceButton", () => {
  it("finds EN advance buttons (Next / Save & Continue)", () => {
    for (const label of ["Next", "Save & Continue", "Save and Continue", "Continue", "Next Step", "Review"]) {
      const scope = scopeWith(`<button>${label}</button>`);
      const hit = findAdvanceButton(scope, null);
      expect(hit?.kind, label).toBe("advance");
    }
  });

  it("finds FR advance buttons (Suivant / Continuer)", () => {
    for (const label of ["Suivant", "Continuer", "Poursuivre"]) {
      const scope = scopeWith(`<button>${label}</button>`);
      expect(findAdvanceButton(scope, null)?.kind, label).toBe("advance");
    }
  });

  it("classifies submit-like buttons as terminal (EN + FR) and never as advance", () => {
    for (const label of ["Submit", "Submit application", "Send application", "Apply now", "Soumettre", "Envoyer", "Postuler", "Terminer", "Poser sa candidature", "Candidater"]) {
      const scope = scopeWith(`<button>${label}</button>`);
      expect(findAdvanceButton(scope, null)?.kind, label).toBe("terminal");
    }
  });

  it("terminal wins when both a Next and a Submit are present", () => {
    const scope = scopeWith(`<button>Next</button><button>Submit</button>`);
    expect(findAdvanceButton(scope, null)?.kind).toBe("terminal");
  });

  it("matches wall verbs only via extraAdvance", () => {
    const scope = scopeWith(`<button>Create Account</button>`);
    expect(findAdvanceButton(scope, null)).toBeNull();
    const hit = findAdvanceButton(scope, null, {
      extraAdvance: /\bcreate( an| my)? account\b/i,
    });
    expect(hit?.kind).toBe("advance");
  });

  it("ignores disabled and aria-disabled buttons", () => {
    const scope = scopeWith(`<button disabled>Next</button><button aria-disabled="true">Continue</button>`);
    expect(findAdvanceButton(scope, null)).toBeNull();
  });

  it("reads input[type=submit] values and [role=button] text", () => {
    const a = scopeWith(`<input type="submit" value="Continue" />`);
    expect(findAdvanceButton(a, null)?.kind).toBe("advance");
    const b = scopeWith(`<div role="button">Next</div>`);
    expect(findAdvanceButton(b, null)?.kind).toBe("advance");
  });

  it("adapter override wins, but its button is still terminal-checked", () => {
    const scope = scopeWith(`<button id="wd">Submit</button><button>Next</button>`);
    const adapter = {
      id: "t", match: () => true,
      advanceButton: (s: HTMLElement) => s.querySelector<HTMLElement>("#wd"),
    } as SiteAdapter;
    const hit = findAdvanceButton(scope, adapter);
    expect(hit?.el.id).toBe("wd");
    expect(hit?.kind).toBe("terminal");
  });
});

/**
 * REGRESSION: on Workday the create-account gate shares a scope with the job
 * posting, whose "Apply" button matches TERMINAL_RE. The flow read that as the
 * final submit, finished "done", and the user was left on the account page with
 * no Continue button.
 */
describe("account wall advance", () => {
  function mountWall(): HTMLElement {
    document.body.innerHTML = `
      <div id="scope">
        <button data-automation-id="adventureButton">Apply</button>
        <input type="password" />
        <button data-automation-id="createAccountSubmitButton">Create Account</button>
      </div>`;
    return document.getElementById("scope")!;
  }

  it("prefers the wall's own button over the posting's Apply", () => {
    const found = findAdvanceButton(mountWall(), null, { extraAdvance: SIGNUP_ADVANCE_RE });
    expect(found).not.toBeNull();
    expect(found!.kind).toBe("advance");
    expect(found!.el.textContent).toBe("Create Account");
  });

  it("still reports a terminal submit on an ordinary form page", () => {
    document.body.innerHTML = `
      <div id="scope">
        <button>Back</button>
        <button>Submit Application</button>
      </div>`;
    const found = findAdvanceButton(document.getElementById("scope")!, null, {});
    expect(found!.kind).toBe("terminal");
  });

  it("does not let a wall regex smuggle past a real submit when no wall is present", () => {
    document.body.innerHTML = `<div id="scope"><button>Submit</button></div>`;
    const found = findAdvanceButton(document.getElementById("scope")!, null, {});
    expect(found!.kind).toBe("terminal");
  });

  it("a real submit still wins when a wall regex is live but no wall button matches", () => {
    document.body.innerHTML = `<div id="scope"><input type="password" /><button>Submit</button></div>`;
    const found = findAdvanceButton(document.getElementById("scope")!, null, { extraAdvance: SIGNUP_ADVANCE_RE });
    expect(found!.kind).toBe("terminal");
  });

  /**
   * SAFETY: on a wall page the controller clicks an "advance" WITHOUT parking
   * for the user (flowController: `if (account.wall)` skips the advance gate).
   * Both regexes are \b-anchored alternations, so one button can carry a wall
   * verb AND a submit verb, classifying that as "advance" would submit the
   * application unreviewed, breaking the never-click-a-terminal invariant.
   */
  it("never returns advance for a button that also reads as a final submit", () => {
    const composites: Array<[string, RegExp]> = [
      ["Register and Submit", SIGNUP_ADVANCE_RE],
      ["Sign up & Submit", SIGNUP_ADVANCE_RE],
      ["Create account and finish", SIGNUP_ADVANCE_RE],
      ["Sign in and submit application", LOGIN_ADVANCE_RE],
      ["S'inscrire et soumettre", SIGNUP_ADVANCE_RE],
    ];
    for (const [label, extraAdvance] of composites) {
      document.body.innerHTML = `<div id="scope"><input type="password" /><button>${label}</button></div>`;
      const found = findAdvanceButton(document.getElementById("scope")!, null, { extraAdvance });
      expect(found!.kind, label).toBe("terminal");
    }
  });

  it("keeps a wall's 'Sign in to apply' an advance, apply is an entry verb, not a final submit", () => {
    document.body.innerHTML = `<div id="scope"><input type="password" /><button>Sign in to apply</button></div>`;
    const found = findAdvanceButton(document.getElementById("scope")!, null, { extraAdvance: LOGIN_ADVANCE_RE });
    expect(found!.kind).toBe("advance");
  });
});

describe("findPendingAdvance", () => {
  // iCIMS's candidate login (live 2026-10-17): Next is disabled until the
  // email and the privacy notice are in, and the page enables it a moment
  // after the fill's events.
  const ICIMS_LOGIN = `
    <form id="enterEmailForm" class="iCIMS_SignIn" method="post">
      <label for="email">Email</label>
      <input type="email" autocomplete="email" id="email" name="css_loginName" class="form-control" value="">
      <label for="accept_gdpr"><input type="checkbox" id="accept_gdpr" name="accept_gdpr" value="1" aria-required="true"> By clicking the Next button below, I agree to the Privacy Notice.</label>
      <input id="enterEmailSubmitButton" type="submit" class="iCIMS_PrimaryButton gdprAnswerSubmitButton" value="Next" disabled="">
    </form>`;

  it("reports the disabled Next the enabled search cannot see", () => {
    const scope = scopeWith(ICIMS_LOGIN);
    expect(findAdvanceButton(scope, null)).toBeNull();
    expect(findPendingAdvance(scope)?.id).toBe("enterEmailSubmitButton");
    (document.getElementById("enterEmailSubmitButton") as HTMLInputElement).disabled = false;
    expect(findAdvanceButton(scope, null)?.el.id).toBe("enterEmailSubmitButton");
  });

  it("ignores disabled buttons that are not a page turn", () => {
    expect(findPendingAdvance(scopeWith(`<button disabled>Remove</button><button aria-disabled="true">Add another</button>`))).toBeNull();
  });
});
