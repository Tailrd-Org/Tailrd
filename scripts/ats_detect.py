"""Which applicant tracking system does an employer's careers page run on?

    python scripts/ats_detect.py https://jobs.lever.co/acme
    python scripts/ats_detect.py https://www.acme.com/careers
    python scripts/ats_detect.py https://www.acme.com/careers --html saved_page.html

Prints a seed entry for backend/data/ats_companies.json on stdout (JSON) and
the evidence on stderr. Exit code 0 when an ATS was found, 1 when the page
shows none, 2 when the page could not be read.

The URL is read first: an ATS board link names its platform and its board
("https://jobs.lever.co/acme" is Lever's board "acme"), and so does a link to
one of its postings. Only when the URL is the employer's own site is the page
fetched, once, the way the crawler fetches anything (TailrdBot user agent,
robots.txt respected, no login, no headless browser). The page's links,
iframes and script tags are then read for the same board links, and for the
script markers the vendors' embeds carry ("teamtailor-cdn.com"). A page you
saved by hand can be read with --html instead of fetching it.

The vendor table is ported from chrome-extension/src/content/siteRegistry.ts,
where the extension already detects most platforms by host; a test keeps the
two in step. The extension's patterns match application pages, so they are
reduced to their hosts here, which also name a careers page. Boards of the
platforms the crawler can read get their slug (and, for Workday, iCIMS and
Oracle, the config the connector needs); any other vendor gets a disabled
entry saying there is no connector yet, as the registry already does.

The entry has no hq_country and no sector: add them (CA or US, and one of the
registry's sectors) before it goes in. company_name is a guess from the page.
"""

from __future__ import annotations

import argparse
import asyncio
import html as htmllib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MAX_PAGE_CHARS = 3_000_000
MAX_URLS = 5000


# ─── What is known about each vendor ─────────────────────────────────────────

@dataclass(frozen=True)
class Vendor:
    platform: str                 # ats_platform in the registry
    label: str
    hosts: tuple[str, ...]        # anchored host suffixes
    markers: tuple[str, ...] = ()  # text an embed leaves in the page source


# Ported from siteRegistry.ts (tier "vendor"): its domains, the hosts of its
# match patterns, and its page-source keywords. Registry names differ from
# the extension's ids for three: oraclecloud -> oracle_hcm, ultipro -> ukg,
# pinpointhq -> pinpoint. Welcome to the Jungle is a job board, not an ATS.
VENDORS: tuple[Vendor, ...] = (
    Vendor("greenhouse", "Greenhouse", ("greenhouse.io",)),
    Vendor("workday", "Workday", ("myworkdayjobs.com", "myworkdayjobs-impl.com",
                                  "myworkdaysite.com", "myworkday.com")),
    Vendor("jibe", "Jibe", ("jibeapply.com",)),
    Vendor("kula", "Kula", ("careers.kula.ai",)),
    Vendor("icims", "iCIMS", ("icims.com",)),
    Vendor("dover", "Dover", ("dover.com",)),
    Vendor("zohorecruit", "Zoho Recruit", ("zohorecruit.com", "zohorecruit.ca", "zohorecruit.eu")),
    Vendor("gem", "Gem", ("jobs.gem.com",)),
    Vendor("hiringthing", "HiringThing", ("hiringthing.com", "oasisrecruit.com", "elevate-ats.com",
                                          "prismhr-hire.com", "gnahiring.com")),
    Vendor("paycomonline", "Paycom", ("paycomonline.com", "paycomonline.net")),
    Vendor("teamtailor", "Teamtailor", ("teamtailor.com", "careers.blueorange.digital",
                                        "careers.totalperform.com"), ("teamtailor-cdn.com",)),
    Vendor("catsone", "CATS", ("catsone.com",)),
    Vendor("ripplehire", "RippleHire", ("ripplehire.com",)),
    Vendor("personio", "Personio", ("personio.de", "personio.com")),
    Vendor("careerspage", "CareersPage", ("careers-page.com",)),
    Vendor("careerplug", "CareerPlug", ("careerplug.com", "sfagentjobs.com", "sfagentcareers.com",
                                        "apscareerportal.com")),
    Vendor("successfactors", "SuccessFactors", ("successfactors.eu", "successfactors.com", "sapsf.com")),
    Vendor("clearcompany", "ClearCompany", ("clearcompany.com",)),
    Vendor("ashby", "Ashby", ("ashbyhq.com",)),
    Vendor("isolved", "isolved", ("isolvedhire.com",)),
    Vendor("jobdiva", "JobDiva", ("jobdiva.com",)),
    Vendor("smartrecruiters", "SmartRecruiters", ("smartrecruiters.com", "smartr.me")),
    Vendor("phenom", "Phenom", (
        "jobs.bswhealth.com", "careers.uvahealth.org", "careers.dukehealth.org", "www.jobs.abbott",
        "careers.aspendental.com", "careers.fivebelow.com", "careers.fourseasons.com", "careers.kbr.com",
        "jobs.kuehne-nagel.com", "careers.mastercard.com", "careers.mcafee.com", "jobs-cee.pwc.com",
        "careers.roche.com", "www.vcacareers.com", "careers.wasteconnections.com"),
        ("APPLY_form_renderer.js", "phenompeople.com")),
    Vendor("lever", "Lever", ("lever.co",)),
    Vendor("jobvite", "Jobvite", ("jobvite.com",)),
    Vendor("breezy", "Breezy", ("breezy.hr",)),
    Vendor("workable", "Workable", ("workable.com", "careers.arbor-education.com")),
    Vendor("gohire", "GoHire", ("jobs.gohire.io",)),
    Vendor("bamboohr", "BambooHR", ("bamboohr.com",)),
    Vendor("brassring", "BrassRing", ("brassring.com",)),
    Vendor("adp", "ADP", ("workforcenow.adp.com", "recruiting.adp.com", "myjobs.adp.com")),
    Vendor("oracle_hcm", "Oracle Cloud Recruiting", ("oraclecloud.com",)),
    Vendor("ukg", "UKG Pro", ("ultipro.com", "ultipro.ca", "rec.pro.ukg.net")),
    Vendor("rippling", "Rippling", ("rippling-ats.com", "ats.rippling.com")),
    Vendor("dayforce", "Dayforce", ("jobs.dayforcehcm.com",)),
    Vendor("taleo", "Taleo", ("taleo.net", "burnsmcd.com", "talentacquisition.3ds.com")),
    Vendor("eightfold", "Eightfold", ("eightfold.ai",), ("eightfold.ai",)),
    Vendor("jazzhr", "JazzHR", ("applytojob.com",)),
    Vendor("trakstar", "Trakstar", ("hire.trakstar.com",)),
    Vendor("freshteam", "Freshteam", ("freshteam.com",)),
    Vendor("pinpoint", "Pinpoint", ("pinpointhq.com",)),
    Vendor("recruitee", "Recruitee", ("recruitee.com",), ("recruitee.com",)),
    Vendor("trinethire", "TriNet Hire", ("app.trinethire.com",)),
    Vendor("jobscore", "JobScore", ("jobscore.com",)),
    Vendor("paylocity", "Paylocity", ("paylocity.com",)),
    Vendor("avature", "Avature", ("avature.net", "careers.arcb.com", "apply.deloitte.com",
                                  "careers.cbre.com", "careers.mantech.com", "careers.ibm.com",
                                  "careers.tql.com"), ("avature.net",)),
    Vendor("comeet", "Comeet", ("comeet.com", "comeet.co")),
    Vendor("polymer", "Polymer", ("jobs.polymer.co",)),
    Vendor("recruiterflow", "Recruiterflow", ("recruiterflow.com",), ("recruiterflow.com",)),
)

_VENDOR_BY_PLATFORM = {vendor.platform: vendor for vendor in VENDORS}


@dataclass(frozen=True)
class BoardRule:
    """A URL that names one employer's board on a platform. ``slug`` is the
    pattern's named group of that name; ``careers`` is the board's own
    address, ``config`` the keys the connector reads, both as templates over
    the pattern's named groups."""
    platform: str
    pattern: re.Pattern
    careers: str
    config: tuple[tuple[str, str], ...] = ()
    cxs: str = ""   # Workday: the CxS base (workday_url_template)


def _rule(platform: str, pattern: str, careers: str, config=(), cxs: str = "") -> BoardRule:
    return BoardRule(platform, re.compile(pattern, re.IGNORECASE), careers, tuple(config), cxs)


_SLUG = r"(?P<slug>[A-Za-z0-9][\w.%-]*)"
_LOCALE = r"(?:/[a-z]{2}-[A-Za-z]{2})?"
# Subdomains of a vendor's own that are never an employer's board (a whole
# label: "careers-guelph.icims.com" is a board).
_NOT_A_TENANT = r"(?!(?:www|api|app|apply|jobs|careers|support|help|login|static|scripts|cdn)\.)"

# Order matters: the first rule that matches a URL names its board.
BOARD_RULES: tuple[BoardRule, ...] = (
    _rule("greenhouse",
          r"^https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_(?:board|app)(?:/js)?\?(?:[^#]*&)?for=" + _SLUG,
          "https://boards.greenhouse.io/{slug}"),
    _rule("greenhouse", r"^https?://boards-api\.greenhouse\.io/v1/boards/" + _SLUG,
          "https://boards.greenhouse.io/{slug}"),
    _rule("greenhouse", r"^https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?!embed\b|v1\b|jobs?\b)" + _SLUG,
          "https://boards.greenhouse.io/{slug}"),
    _rule("lever", r"^https?://api(?:\.eu)?\.lever\.co/v0/postings/" + _SLUG, "https://jobs.lever.co/{slug}"),
    _rule("lever", r"^https?://(?P<host>jobs(?:\.eu)?\.lever\.co)/" + _SLUG, "https://{host}/{slug}"),
    _rule("ashby", r"^https?://api\.ashbyhq\.com/posting-api/job-board/" + _SLUG, "https://jobs.ashbyhq.com/{slug}"),
    _rule("ashby", r"^https?://jobs\.ashbyhq\.com/" + _SLUG, "https://jobs.ashbyhq.com/{slug}"),
    _rule("smartrecruiters", r"^https?://api\.smartrecruiters\.com/v1/companies/" + _SLUG,
          "https://jobs.smartrecruiters.com/{slug}"),
    _rule("smartrecruiters", r"^https?://(?:jobs|careers)\.smartrecruiters\.com/(?!oneclick-ui\b)" + _SLUG,
          "https://jobs.smartrecruiters.com/{slug}"),
    _rule("workday",
          r"^https?://(?P<tenant>[\w-]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com" + _LOCALE
          + r"/(?!job\b|wday\b|[a-z]{2}-[A-Za-z]{2}(?:[/?#]|$))(?P<site>[^/?#]+)",
          "", cxs="https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"),
    _rule("workday",
          r"^https?://(?P<pod>wd\d+)\.myworkdaysite\.com" + _LOCALE + r"/recruiting/(?P<tenant>[^/?#]+)/(?P<site>[^/?#]+)",
          "", cxs="https://{pod}.myworkdaysite.com/wday/cxs/{tenant}/{site}"),
    _rule("workable", r"^https?://apply\.workable\.com/api/v\d/(?:widget/)?accounts/" + _SLUG,
          "https://apply.workable.com/{slug}/"),
    _rule("workable", r"^https?://apply\.workable\.com/(?!api\b)" + _SLUG, "https://apply.workable.com/{slug}/"),
    _rule("recruitee", r"^https?://" + _NOT_A_TENANT + r"(?P<slug>[A-Za-z0-9][\w-]*)\.recruitee\.com",
          "https://{slug}.recruitee.com"),
    _rule("teamtailor",
          r"^https?://" + _NOT_A_TENANT + r"(?P<host>(?P<slug>[A-Za-z0-9][\w-]*)(?:\.na)?\.teamtailor\.com)",
          "https://{host}"),
    _rule("bamboohr", r"^https?://" + _NOT_A_TENANT + r"(?P<slug>[A-Za-z0-9][\w-]*)\.bamboohr\.com/(?:careers|jobs)",
          "https://{slug}.bamboohr.com/careers"),
    _rule("jobvite", r"^https?://jobs\.jobvite\.com/(?:careers/)?" + _SLUG, "https://jobs.jobvite.com/{slug}"),
    _rule("icims", r"^https?://" + _NOT_A_TENANT + r"(?P<host>(?P<slug>[A-Za-z0-9][\w-]*)\.icims\.com)",
          "https://{host}/jobs/search", config=(("host", "{host}"),)),
    _rule("oracle_hcm",
          r"^https?://(?P<host>[\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?"
          r"sites/(?P<site>[\w-]+)",
          "https://{host}/hcmUI/CandidateExperience/en/sites/{site}",
          config=(("host", "{host}"), ("site", "{site}"))),
)


@dataclass
class Hit:
    """One piece of evidence: a platform, and when the evidence names a board
    (a URL does, a script marker does not) which board."""
    platform: str
    slug: str = ""
    careers_url: str = ""
    config: dict = field(default_factory=dict)
    cxs: str = ""
    source: str = "url"           # url | link | marker
    evidence: str = ""
    page_url: str = ""            # the address of the page the evidence was read on

    @property
    def board(self) -> bool:
        """The evidence names one employer's board (an Oracle site has a
        host and a site name, no slug)."""
        return bool(self.slug or self.config)

    @property
    def key(self) -> tuple:
        return (self.platform, self.slug, tuple(sorted(self.config.items())))


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _clean_url(url: str) -> str:
    return htmllib.unescape(url).replace("\\/", "/").strip()


def board_hit(url: str) -> Hit | None:
    """The board a URL names, for the platforms the registry knows how to
    write down, or None."""
    from backend.services.ats_scraper import workday_public_base

    url = _clean_url(url)
    for rule in BOARD_RULES:
        match = rule.pattern.search(url)
        if not match:
            continue
        parts = {key: value for key, value in match.groupdict().items() if value}
        config = {key: template.format(**parts) for key, template in rule.config}
        if rule.cxs:
            cxs = rule.cxs.format(**parts)
            return Hit(rule.platform, parts["tenant"], workday_public_base(cxs), cxs=cxs,
                       evidence=url)
        slug = parts.get("slug", "")
        if rule.platform == "teamtailor" and parts["host"].lower() != f"{slug.lower()}.teamtailor.com":
            config["host"] = parts["host"]  # the North American data centre
        return Hit(rule.platform, slug, rule.careers.format(**parts), config, evidence=url)
    return None


# Query parameters an employer's own page carries when its jobs come from
# the vendor: "?gh_jid=4285367" is a Greenhouse job, "?ashby_jid=" an Ashby one.
QUERY_MARKERS = {"gh_jid": "greenhouse", "ashby_jid": "ashby"}


def vendor_hit(url: str) -> Hit | None:
    """The vendor a URL is on, or carries a job of, without a board: a link
    to a vendor's page that names no employer, or one the rules do not read."""
    parts = urlsplit(_clean_url(url))
    host = (parts.hostname or "").lower()
    if not host:
        return None
    for vendor in VENDORS:
        if any(_host_matches(host, suffix.lower()) for suffix in vendor.hosts):
            return Hit(vendor.platform, evidence=_clean_url(url))
    query = parse_qs(parts.query)
    for parameter, platform in QUERY_MARKERS.items():
        if parameter in query:
            return Hit(platform, evidence=_clean_url(url))
    return None


def url_hit(url: str) -> Hit | None:
    return board_hit(url) or vendor_hit(url)


# ─── Reading a page ──────────────────────────────────────────────────────────

_URL = re.compile(r"""(?:https?:)?//[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?::\d+)?(?:/[^\s"'<>\\)\]}]*)?""")


def page_urls(html: str) -> list[str]:
    """Every absolute or protocol-relative URL in the page, in order: links,
    iframes, script and form sources, and the ones inline scripts and JSON
    name (with their escaped slashes read back). The first MAX_URLS."""
    text = (html or "")[:MAX_PAGE_CHARS].replace("\\/", "/")
    urls = []
    for found in _URL.findall(text):
        found = found.rstrip(".,;:")
        urls.append("https:" + found if found.startswith("//") else found)
        if len(urls) >= MAX_URLS:
            break
    return urls


def page_hits(html: str) -> list[Hit]:
    hits = []
    for url in page_urls(html):
        hit = url_hit(url)
        if hit:
            hit.source = "link"
            hits.append(hit)
    for vendor in VENDORS:
        for marker in vendor.markers:
            if marker in html:
                hits.append(Hit(vendor.platform, source="marker", evidence=marker))
                break
    return hits


_SOURCE_RANK = {"url": 0, "link": 1, "marker": 2}


def _ranked(hits: list[Hit]) -> list[tuple[Hit, int]]:
    """Distinct (platform, board) pairs with their counts, the likeliest
    first: a board beats a bare vendor, then the URL given beats the page's
    links and the page's links beat its script markers, then more mentions,
    then the page's own order."""
    counts: dict[tuple, int] = {}
    first: dict[tuple, Hit] = {}
    for hit in hits:
        counts[hit.key] = counts.get(hit.key, 0) + 1
        first.setdefault(hit.key, hit)
    order = {key: index for index, key in enumerate(first)}
    keys = sorted(first, key=lambda key: (not first[key].board, _SOURCE_RANK[first[key].source],
                                          -counts[key], order[key]))
    return [(first[key], counts[key]) for key in keys]


def crawlable_platforms() -> set[str]:
    """Platforms this checkout can crawl: the built-in five and every
    platform that has a connector in backend/ingest/adapters."""
    from backend.data.company_registry import SUPPORTED_PLATFORMS
    from backend.ingest.adapters import adapter_platforms

    return set(SUPPORTED_PLATFORMS) | set(adapter_platforms())


# ─── The registry entry ──────────────────────────────────────────────────────

_PREFIX_LABELS = {"www", "careers", "career", "jobs", "job", "apply", "join", "talent", "recruiting",
                  "hiring", "work", "en", "fr"}
_SECOND_LEVELS = {"co", "com", "org", "gov", "edu", "ac", "net"}
# A <title> that names no employer.
_GENERIC_TITLES = {"jobs", "job", "careers", "career", "home", "join us", "work with us", "open roles",
                   "open positions", "current openings", "job openings", "search jobs"}
_TITLE_NOISE = re.compile(
    r"^(?:careers?|jobs?|join)\s+(?:at|with)\s+|\s*[-|:–]\s*(?:careers?|jobs?|work with us|current openings"
    r"|job openings|open positions).*$", re.IGNORECASE)


def host_slug(url: str) -> str:
    """"aircanada" for careers.aircanada.com: the employer's name in the
    host of its own site."""
    labels = [label for label in (urlsplit(url).hostname or "").lower().split(".") if label]
    while len(labels) > 2 and labels[0] in _PREFIX_LABELS:
        labels.pop(0)
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVELS:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else ""


def page_name(html: str) -> str:
    """The employer's name as the page gives it: og:site_name, else the
    <title> without its "Careers at" / "- Jobs" decoration."""
    for tag in re.finditer(r"<meta\b([^>]*)>", html or "", re.IGNORECASE):
        attrs = {key.lower(): value for key, value in re.findall(r"""([\w:-]+)\s*=\s*["']([^"']*)["']""", tag.group(1))}
        if attrs.get("property", "").lower() == "og:site_name" and attrs.get("content", "").strip():
            return htmllib.unescape(attrs["content"]).strip()
    title = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    if title:
        name = _TITLE_NOISE.sub("", htmllib.unescape(" ".join(title.group(1).split()))).strip()
        return "" if name.lower() in _GENERIC_TITLES else name
    return ""


_LOCALE_SEGMENT = re.compile(r"[a-z]{2}(?:-[A-Za-z]{2})?")
_GENERIC_SEGMENTS = {"jobs", "job", "careers", "career", "apply", "search", "o", "p", "positions",
                     "postings", "openings", "companies", "company"}


def _first_segment(path: str) -> str:
    """The first path segment that could name an employer: it starts with a
    letter (not a posting id like "123-welder"), is no locale, and is not a
    word every careers site has."""
    for segment in path.split("/"):
        if (re.match(r"[A-Za-z]", segment) and segment.lower() not in _GENERIC_SEGMENTS
                and not _LOCALE_SEGMENT.fullmatch(segment)):
            return segment
    return ""


def guess_slug(hit: Hit, page_url: str) -> str:
    """A board slug for a hit that names none, which the registry needs all
    the same: the leftmost label of a vendor subdomain ("acme" for
    acme.applytojob.com); on a vendor's own host the employer in the path
    ("index-exchange" for jobs.gem.com/index-exchange); else the employer's
    name in the page's own host."""
    parts = urlsplit(page_url if hit.source == "marker" else _clean_url(hit.evidence))
    host = (parts.hostname or "").lower()
    vendor = _VENDOR_BY_PLATFORM.get(hit.platform)
    for suffix in (vendor.hosts if vendor else ()):
        suffix = suffix.lower()
        if host.endswith("." + suffix):
            label = host[: -len(suffix) - 1].split(".")[0]
            if label not in _PREFIX_LABELS:
                return label
            return _first_segment(parts.path) or host_slug(page_url)
        if host == suffix:
            return _first_segment(parts.path) or host_slug(page_url)
    return host_slug(page_url)


@dataclass
class Detection:
    hit: Hit
    others: list[tuple[Hit, int]]
    entry: dict
    supported: bool
    notes: list[str]


def build_entry(hit: Hit, html: str = "", name: str = "",
                supported: set[str] | None = None) -> Detection:
    """The seed entry for a hit. Enabled only when this checkout can crawl the
    platform (and, for Workday, the entry carries its CxS base); otherwise it
    is disabled with a reason, as the registry's other unsupported boards are."""
    supported = crawlable_platforms() if supported is None else supported
    page_url = hit.page_url
    vendor = _VENDOR_BY_PLATFORM.get(hit.platform)
    label = vendor.label if vendor else hit.platform
    notes = []

    slug = hit.slug
    if not slug:
        slug = guess_slug(hit, page_url)
        notes.append(f"board_slug '{slug}' is a guess: {label} was found, not the employer's board name")
    careers_url = hit.careers_url or page_url.split("#")[0]
    config = dict(hit.config)
    if hit.platform == "teamtailor" and not config:
        # A career site on the employer's own domain: the connector reads the
        # feed from that host.
        host = urlsplit(page_url if hit.source == "marker" else _clean_url(hit.evidence)).hostname or ""
        if host and not host.endswith("teamtailor.com"):
            config["host"] = host
    if hit.platform == "workday" and not hit.cxs:
        notes.append("Workday was found but not the site name: set workday_url_template to "
                     "https://<tenant>.wd<N>.myworkdayjobs.com/wday/cxs/<tenant>/<site> "
                     "(the site is the first path segment of an open posting)")

    company = name or page_name(html) or slug.replace("-", " ").replace("_", " ").title()
    entry: dict = {"company_name": company, "ats_platform": hit.platform, "board_slug": slug}
    if hit.cxs:
        entry["workday_url_template"] = hit.cxs
    if config:
        entry["config"] = config
    entry["careers_url"] = careers_url
    can_crawl = hit.platform in supported and (hit.platform != "workday" or bool(hit.cxs))
    entry["enabled"] = can_crawl
    if not can_crawl:
        entry["disabled_reason"] = (
            f"{label} career site; host {urlsplit(careers_url).hostname or ''}; "
            f"no connector for {hit.platform} yet.")
    if not name:
        notes.append("company_name is a guess" + (" from the page" if page_name(html) else "") + ": check it")
    notes.append("add hq_country (CA or US) and sector before this goes in the registry")
    return Detection(hit, [], entry, can_crawl, notes)


def detect(url: str, html: str = "", *, name: str = "", final_url: str = "",
           supported: set[str] | None = None) -> Detection | None:
    """The platform behind ``url``, from the URL alone and then, when the
    page's HTML is given, from the page too. ``final_url`` is where fetching
    the page ended up after redirects. A board named by the URL wins; a URL
    that only names a vendor ("?gh_jid=") competes with what the page shows.
    None when nothing in either names an ATS."""
    hits = []
    for address in dict.fromkeys(a for a in (url, final_url) if a):
        hit = url_hit(address)
        if hit:
            hit.source, hit.page_url = "url", address
            hits.append(hit)
    for hit in page_hits(html) if html else []:
        hit.page_url = final_url or url
        hits.append(hit)
    ranked = _ranked(hits)
    if not ranked:
        return None
    detection = build_entry(ranked[0][0], html, name, supported)
    detection.others = ranked[1:]
    return detection


# ─── Fetching ────────────────────────────────────────────────────────────────

class FetchError(Exception):
    """The page could not be read; the message says why."""


async def _fetch(url: str) -> tuple[str, str]:
    import httpx

    from backend.ingest.core import http

    async with http.make_client() as client:
        if not await http.robots_allowed(client, url):
            raise FetchError(f"robots.txt does not allow TailrdBot to read {url}")
        try:
            response = await http.request(client, "GET", url, retries=1, headers={"Accept": "text/html"})
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__} fetching {url}") from exc
        if response.status_code >= 400:
            raise FetchError(f"{url} answered HTTP {response.status_code}")
        return str(response.url), response.text[:MAX_PAGE_CHARS]


def fetch_page(url: str) -> tuple[str, str]:
    """(final URL, HTML) of a page, politely; raises FetchError."""
    return asyncio.run(_fetch(url))


# ─── Command line ────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/ats_detect.py",
        description="Detect the ATS behind a careers page and print a registry entry.")
    parser.add_argument("url", help="the careers page, or a board or posting link of an ATS")
    parser.add_argument("--html", metavar="FILE", help="read the page from this saved file instead of fetching it")
    parser.add_argument("--name", default="", help="company_name for the entry (default: a guess from the page)")
    parser.add_argument("--no-fetch", action="store_true", help="read the URL only, never the page")
    args = parser.parse_args(argv)

    detection = detect(args.url, name=args.name)
    if detection is None or not detection.hit.board:
        # The URL names no board (the employer's own site, or a vendor's page
        # that does not say whose): the page may.
        final_url, html = args.url, ""
        if args.html:
            html = Path(args.html).read_text(encoding="utf-8", errors="replace")[:MAX_PAGE_CHARS]
        elif not args.no_fetch:
            try:
                final_url, html = fetch_page(args.url)
            except FetchError as exc:
                print(f"cannot read the page: {exc}", file=sys.stderr)
                if detection is None:
                    return 2
        if html:
            detection = detect(args.url, html, name=args.name, final_url=final_url) or detection

    if detection is None:
        print("no ATS found in the URL or the page", file=sys.stderr)
        return 1

    hit = detection.hit
    vendor = _VENDOR_BY_PLATFORM.get(hit.platform)
    print(f"found {vendor.label if vendor else hit.platform} ({hit.platform}) from {hit.source}: {hit.evidence}",
          file=sys.stderr)
    for other, count in detection.others[:4]:
        print(f"also seen: {other.platform}" + (f" board {other.slug}" if other.slug else "")
              + f" x{count} ({other.evidence})", file=sys.stderr)
    if not detection.supported:
        print(f"{hit.platform} has no connector in this checkout: the entry is disabled", file=sys.stderr)
    for note in detection.notes:
        print(f"note: {note}", file=sys.stderr)
    print(json.dumps(detection.entry, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
