/**
 * A LIVE Greenhouse application embedded the way company career sites do it:
 * the employer's own page holds a cross-origin <iframe> onto Greenhouse's
 * embed endpoint (`grnhse_iframe`, /embed/job_app?for=<board>&token=<job>).
 * The panel lives in the top frame; every field lives in the child frame.
 *
 * The host page is served locally at a made-up employer URL (route
 * interception, nothing resolves it); the iframe loads the real Greenhouse
 * embed. Same posting and expectations as live-gh-oneimaging.
 */
import liveCases from "./real-live.mjs";

const base = liveCases.find((c) => c.id === "live-gh-oneimaging");

const HOST_PAGE = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Careers | One Imaging</title></head>
<body>
  <header><h1>Careers at One Imaging</h1></header>
  <main>
    <h2>Software Engineer Intern</h2>
    <p>Apply below.</p>
    <div id="grnhse_app">
      <iframe id="grnhse_iframe" title="Greenhouse Job Board"
        src="https://job-boards.greenhouse.io/embed/job_app?for=oneimaging&amp;token=4403125009"
        style="width:100%;height:3200px;border:0" scrolling="no"></iframe>
    </div>
  </main>
</body>
</html>`;

// Retired 2026-10-08 (Greenhouse's job API answered 404), restored 2026-10-17:
// the posting is open again.
export default [
  {
    ...base,
    id: "live-gh-oneimaging-embedded",
    url: "https://careers.oneimaging.example/jobs/4403125009",
    html: HOST_PAGE,
  },
];
