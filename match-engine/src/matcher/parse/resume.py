"""Résumé assembly: text, files and Tailrd's stored documents into the engine's `Resume`.

SPEC §14.1-§14.3, §15; DECISIONS D-11, D-12, D-27, D-28, D-32..D-36, D-41, D-61, D-62, D-80.

`parse_resume_text(text, ...)` normalises the text as ingest does (`normalize_text`; given `lines`
from ingest are used as they are), checks the language, segments it (`sections.segment`) and reads
each section by its kind:

* EXPERIENCE and VOLUNTEER: roles (`roles.split_roles` + `roles.build_role`). Roles come ONLY from
  these sections, or from the date-anchored blocks `segment` falls back to when a résumé has no
  header; never from unstructured text. The employer is `build_role`'s (a header-line tech stack
  is no employer). The experience text is cut after `ingest.max_experience_tokens` whitespace
  tokens, and after 300 roles (the model's bound), flag TRUNCATED. A section that yields no role at
  all (bullets with no title line) is read as skills-blob prose, never as a role.
* PROJECTS: one `Project` per entry: a name line (its date range, when it has one, dates the
  project and leaves the name) and its bullets. The name line and the bullets give ROLE_BULLET or
  ROLE_BULLET_QUANTIFIED mentions with their verb class and polarity (`analyze_bullet`), source
  PROJECT, undated (the project's end dates them, D-34).
* SKILLS and LANGUAGES (programming and spoken languages are both skill nodes): each line read as a
  list (`extract(context="list")`) after its "Category:" label, polarity from `analyze_bullet`
  ("Currently learning: Rust" is aspirational), SKILLS_BLOB. List items that resolve to nothing are
  `unresolved_tokens`.
* CERTIFICATIONS: one entry per line, lines with no mention (issuer, date, status) and bullets
  joining the entry above; credential nodes are CERTIFICATION evidence, other mentions SKILLS_BLOB,
  source CERTIFICATION.
* EDUCATION: `education.parse_education` (with `as_of`), coursework lines ("Relevant Coursework:
  ...", and the lines that continue them) as ROLE_BULLET mentions of verb EXPOSURE, source
  EDUCATION (SPEC §15.1 "coursework in"; never role evidence: the `Resume` has no role for them, so
  they sit in the skills blob container); other lines that name no degree go to the skills blob.
  COURSEWORK sections are coursework lines.
* SUMMARY, AWARDS, OTHER, CONTACT and text outside any section: SKILLS_BLOB with the polarity of
  `analyze_bullet` ("seeking a role in machine learning" is ASPIRATIONAL), read per paragraph.
  The first line of the document is the person's name when it looks like one and gives nothing;
  e-mail, phone, URL and place pieces of contact lines give nothing either.
* PUBLICATIONS, INTERESTS and REFERENCES give no skills (SPEC §14.3).

Credentials (taxonomy D-28, DECISIONS D-28a): a status word standing next to a credential's own
mention ("CPA, in progress", "RN (pending NCLEX)", "RN students", "Pursuing a CPA designation") turns
it into the credential's formal in-progress designation (`cred.cpa` -> `cred.cpa_in_progress`), or
drops it when the credential has none, so the licence gate (D-62) never passes an unlicensed
candidate; a status that belongs to a degree, a place or another skill ("Registered Nurse (RN),
Master of Nursing (expected 2027)") marks nothing, so it never fails a licensed one. See
`credential_status`. Every mention the parser reads goes through this, role titles and bullets
included. Blank text, and a stored document with nothing in it, is NOT_A_RESUME.

Locations come only from contact lines (the CONTACT section; without headers, the first lines of
the document): "City, Region[, Country]", "City, Country", a known city, a region or a country,
never a street or postal code (SPEC §9). Work authorisation from its phrases outside experience,
projects and publications: "Canadian citizen", "US citizen", "permanent resident" (of the country it
names, else of the one country the contact places name), "open work permit" / PGWP (Canada),
"authorized to work in <countries>", "require(s) sponsorship" and its negations.

Flags: those given (ingest), NO_SECTIONS, the role and project date flags (IMPRECISE_DATE, ...),
TRUNCATED, and FUNCTIONAL_RESUME when the résumé has skills or summary content and experience
entries but no dated role, or when its "Summary of Qualifications" / "Areas of Expertise" style
sections hold at least half its lines and no role is dated (SPEC §14.3). An unsupported language is
the reason UNSUPPORTED_LANGUAGE and no résumé: the given language (ingest's), else the majority
language of the lines, an unsupported one deciding only with three lines or more (`_text_language`). `total_experience_months` is the union of the dated
roles' intervals (None when no role is dated).

`parse_resume_bytes` is `ingest_bytes` then `parse_resume_text`; an ingest failure is its reasons.
`resume_from_document` reads Tailrd's stored résumé document (D-80) through the same helpers, so a
document and the same résumé written as text agree; a document with no experience, education,
projects or skills content falls back to `raw_text`. Nothing here raises unless `strict=True`:
non-text input is INVALID_INPUT and an internal fault is INTERNAL_ERROR, both with no résumé.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple, Optional, Union

from pydantic import model_validator

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.loader import load_graph, load_resolver_settings
from matcher.ingest import TextLine, ingest_bytes
from matcher.ingest.language import SUPPORTED_LANGUAGES, detect_line_language
from matcher.ingest.normalize_text import normalize_text
from matcher.models import (
    DateValue,
    Education,
    Evidence,
    Frozen,
    Location,
    ParseTier,
    Polarity,
    Project,
    Resume,
    Role,
    SkillKind,
    SkillRef,
    SkillSource,
    VerbClass,
    WorkAuth,
)
from matcher.normalize.resolver import Mention, Resolver
from matcher.parse import _entries
from matcher.parse._text import fold
from matcher.parse.config import ParseConfig, SectionKind, header_key
from matcher.parse.dates import DateRange, find_date_ranges, months_between, parse_range
from matcher.parse.education import degree_matches, parse_education
from matcher.parse.roles import RoleDraft, build_role, split_roles
from matcher.parse.scope import ScopeFeatures, extract_scope
from matcher.parse.sections import Section, classify_header, segment
from matcher.parse.taxonomy_extras import MosIndex, TitleIndex, load_mos_index, load_title_index
from matcher.score.evidence import BulletMention, analyze_bullet, is_quantified

__all__ = [
    "ParsedResume",
    "ResumeResources",
    "credential_status",
    "default_taxonomy_dir",
    "load_resume_resources",
    "parse_resume_bytes",
    "parse_resume_text",
    "resume_from_document",
]

_logger = logging.getLogger(__name__)

# models.Resume bounds
_MAX_ROLES = 300
_MAX_PROJECTS = 300
_MAX_EDUCATION = 50
_MAX_CERTS = 300
_MAX_BLOB = 5000
_MAX_SKILLS = 1000  # per role / project
_MAX_LOCATIONS = 20
_MAX_TEXT = 2000
_MAX_TOKEN = 200
_MAX_TOKENS = 1000
_MAX_MONTHS = 1200.0

_EXPERIENCE_LIKE = (SectionKind.EXPERIENCE, SectionKind.VOLUNTEER)
_NO_SKILLS = (SectionKind.PUBLICATIONS, SectionKind.INTERESTS, SectionKind.REFERENCES)
_NO_WORK_AUTH = (SectionKind.EXPERIENCE, SectionKind.VOLUNTEER, SectionKind.PROJECTS, SectionKind.PUBLICATIONS)
_QUALIFICATION_WORDS = ("qualification", "expertise", "competenc")


# ─── output ─────────────────────────────────────────────────────────────────────────────────


class ParsedResume(Frozen):
    """A parsed résumé, or the reasons there is none; plus what the parse saw (diagnostics)."""

    resume: Optional[Resume]
    reasons: tuple[Flag, ...] = ()
    flags: tuple[Flag, ...] = ()
    unresolved_tokens: tuple[str, ...] = ()
    unmapped_titles: tuple[str, ...] = ()
    scope: tuple[ScopeFeatures, ...] = ()  # one per role, in role order
    sections: tuple[SectionKind, ...] = ()
    parse_config_version: str = ""

    @model_validator(mode="after")
    def _resume_or_reasons(self) -> "ParsedResume":
        if (self.resume is None) != bool(self.reasons):
            raise ValueError("a ParsedResume has reasons exactly when it has no résumé")
        return self


def _ordered(flags: Iterable[Flag]) -> tuple[Flag, ...]:
    present = set(flags)
    return tuple(flag for flag in Flag if flag in present)


def _failed(reason: Flag, parse_config: Any, flags: Iterable[Flag] = ()) -> ParsedResume:
    version = getattr(parse_config, "version", "")
    return ParsedResume(resume=None, reasons=(reason,), flags=_ordered(flags),
                        parse_config_version=version if isinstance(version, str) else "")


# ─── taxonomy resources ─────────────────────────────────────────────────────────────────────


class ResumeResources(NamedTuple):
    """What the parser reads from the taxonomy besides the resolver: job titles, military codes,
    each credential's formal in-progress designation (D-28) and which ids are credentials."""

    titles: TitleIndex
    mos: MosIndex
    in_progress: Mapping[str, str]  # licence or charter id -> its in-progress designation id
    credentials: frozenset[str]  # ids of CREDENTIAL nodes
    parse_config: Optional[ParseConfig] = None  # degree words: a degree stops a status reading

    @property
    def designations(self) -> frozenset[str]:
        return frozenset(self.in_progress.values())


def default_taxonomy_dir() -> Path:
    # src/matcher/parse/resume.py -> match-engine/data/taxonomy
    return Path(__file__).resolve().parents[3] / "data" / "taxonomy"


_RESOURCES: dict[tuple[str, str], ResumeResources] = {}
_RESOURCES_LOCK = threading.Lock()


def load_resume_resources(taxonomy_dir: Union[str, Path, None] = None, *,
                          parse_config: ParseConfig) -> ResumeResources:
    """The taxonomy resources for `parse_config` (cached per directory and parse config)."""
    root = Path(taxonomy_dir) if taxonomy_dir is not None else default_taxonomy_dir()
    key = (str(root.resolve()), parse_config.version)
    with _RESOURCES_LOCK:
        cached = _RESOURCES.get(key)
        if cached is not None:
            return cached
        graph = load_graph(root)
        settings = load_resolver_settings(root)
        resources = ResumeResources(
            titles=load_title_index(root, parse_config=parse_config),
            mos=load_mos_index(root),
            in_progress=dict(settings.get("in_progress", {})),
            credentials=frozenset(node_id for node_id, node in graph.nodes.items()
                                  if node.kind is SkillKind.CREDENTIAL),
            parse_config=parse_config,
        )
        _RESOURCES[key] = resources
        return resources


# ─── credential status (taxonomy D-28) ──────────────────────────────────────────────────────

# Status words by where they stand (taxonomy D-28; DECISIONS D-28a). NOUN words name the person
# and count only right after the credential ("RN student", "CPA candidate", "309A) Apprentice"); POST
# words follow it ("CPA, in progress", "RN (pending NCLEX)", "Registered Nurse, writing the NCLEX");
# PRE words lead into it ("Pursuing a CPA designation", "enrolled in RN bridging").
_NOUN_STATUS = (r"candidates?|students?|trainees?|apprentices?|apprenticeship|candidat(?:e|s|es)?|"
                r"[ée]tudiant(?:e|s|es)?|stagiaires?|apprentie?s?")
_POST_STATUS = (r"in[\s-]+progress|pending|expected|anticipated|in[\s-]+training|writing\s+the|"
                r"level\s+(?:iii|ii|i|[123])|en\s+cours|en\s+formation|pr[ée]vue?s?")
_PRE_STATUS = r"currently\s+completing|completing|pursuing|working\s+towards?|currently\s+enrolled|enrolled|studying\s+for"
_STATUS = re.compile(rf"(?<![\w])(?:(?P<noun>{_NOUN_STATUS})|(?P<post>{_POST_STATUS})|(?P<pre>{_PRE_STATUS}))"
                     r"(?![\w])", re.IGNORECASE)
_GAP_WORD = re.compile(r"[^\W_]+")
_GAP_HARD_STOP = re.compile(r"[;|•·]")
_ACRONYM = re.compile(r"[A-Z0-9][A-Z0-9.]{0,6}")
_NOUN_GAP_CHARS = re.compile(r"[^\w\s()\[\].\-]")
_PRE_GAP_CHARS = re.compile(r"[^\w\s]")
# Words that may stand between a credential and a status after it: connectors, the credential's own
# program word ("Primary Care Paramedic Diploma"), months and years; capitalised words (an issuer,
# "Centennial College") pass too. A degree ("Master of Nursing", "BScN"), another skill or any other
# lower-case word ("renewal", "endorsement") stops the reading.
_GAP_CONNECTORS = frozenset({"of", "and", "the", "de", "du", "des", "la", "le", "et", "at", "from", "by"})
_GAP_PROGRAM = frozenset({"diploma", "program", "programme", "certificate", "certification", "designation",
                          "exam", "examination", "licence", "license", "licensing", "course", "registration"})
_MONTHS = frozenset({"jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june",
                     "jul", "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov",
                     "november", "dec", "december", "spring", "summer", "fall", "autumn", "winter"})
_PRE_GAP = frozenset({"a", "an", "the", "my", "in", "for", "to", "towards", "toward", "of", "un", "une", "le",
                      "la", "les", "du", "de"})


def _degree_spans(text: str, resources: "ResumeResources") -> list[tuple[int, int]]:
    if resources.parse_config is None:
        return []
    return [(found.start, found.end) for found in degree_matches(text, resources.parse_config)]


def _noun_gap(gap: str) -> bool:
    """Only brackets, a period, a hyphen or an acronym ("(PCP)", "-A") between a credential and a
    noun status (or between two mentions of one credential, "Registered Nurse (RN)")."""
    if "\n" in gap or _NOUN_GAP_CHARS.search(gap):
        return False
    return all(_ACRONYM.fullmatch(word) for word in _GAP_WORD.findall(gap))


def _post_gap(text: str, start: int, end: int, degrees: Sequence[tuple[int, int]]) -> bool:
    """Only punctuation, issuer, date and program words between a credential and a status after it."""
    gap = text[start:end]
    if _GAP_HARD_STOP.search(gap):
        return False
    for word in _GAP_WORD.finditer(gap):
        token = word.group()
        folded = fold(token)
        if folded in _GAP_CONNECTORS or folded in _GAP_PROGRAM or folded in _MONTHS or token.isdigit():
            continue
        offset = start + word.start()
        if any(left <= offset < right for left, right in degrees) or not token[:1].isupper():
            return False
    return True


def _pre_gap(gap: str) -> bool:
    return "\n" not in gap and not _PRE_GAP_CHARS.search(gap) and all(
        fold(word) in _PRE_GAP for word in _GAP_WORD.findall(gap))


def credential_status(text: str, mentions: Sequence[Mention], resources: ResumeResources) -> tuple[Mention, ...]:
    """`mentions` of `text` with credential status read (taxonomy D-28; DECISIONS D-28a).

    A status word marks a credential only when it is next to that credential's own mention:
    * inside it ("Professional Engineer in training");
    * a NOUN word ("student", "candidate", "trainee", "apprentice") right after it, with only
      brackets, a hyphen or an acronym between ("RN students", "Primary Care Paramedic (PCP)
      student", "CDL-A trainee", "Electrician (309A) Apprentice");
    * a POST word ("in progress", "pending", "expected", "writing the", "Level II") after it, with
      only punctuation (never ; | •), connectors, issuer names, dates or the credential's program
      word between ("CPA, in progress", "RN (pending NCLEX)", "Primary Care Paramedic Diploma,
      Centennial College (expected 2026)"); a degree ("Master of Nursing", "BScN"), another skill or
      another lower-case word between ("renewal pending", "Hazmat endorsement pending") stops it;
    * a PRE word ("pursuing", "completing", "enrolled") before it with only articles and
      prepositions between ("Pursuing a CPA designation", "enrolled in RN bridging").
    The mention marked is the nearest one, with the mentions of the same credential right before it
    ("Registered Nurse (RN) candidate"). A marked credential becomes its formal in-progress
    designation, stays when it is one already ("CPA candidate", "CFA Level I"), and is dropped when it
    has none ("AWS Certified ... (expected 2026)"). Other mentions are returned as they are, in order.
    A status next to no credential marks nothing, so a held licence is never read as pursued because
    of a degree, a place, another skill or another credential's status."""
    credentials = resources.credentials
    if not any(m.canonical_id in credentials for m in mentions):
        return tuple(mentions)
    ordered = sorted(range(len(mentions)), key=lambda i: (mentions[i].start, mentions[i].end))
    degrees = _degree_spans(text, resources)
    marked: set[int] = set()

    def mark_chain(position: int) -> None:
        first = mentions[ordered[position]]
        marked.add(ordered[position])
        cursor = first.start
        for k in range(position - 1, -1, -1):
            m = mentions[ordered[k]]
            if m.canonical_id != first.canonical_id or not _noun_gap(text[m.end:cursor]):
                break
            marked.add(ordered[k])
            cursor = m.start

    for status in _STATUS.finditer(text):
        s, e = status.start(), status.end()
        inside = [i for i, m in enumerate(mentions) if m.start <= s < m.end and m.canonical_id in credentials]
        if inside:
            marked.update(inside)
            continue
        if status.group("pre"):
            after = [k for k, i in enumerate(ordered) if mentions[i].start >= e]
            if after:
                m = mentions[ordered[after[0]]]
                if m.canonical_id in credentials and _pre_gap(text[e:m.start]):
                    marked.add(ordered[after[0]])
            continue
        before = [k for k, i in enumerate(ordered) if mentions[i].end <= s]
        if not before:
            continue
        nearest = before[-1]
        m = mentions[ordered[nearest]]
        if m.canonical_id not in credentials:
            continue  # another skill stands between ("CPA, Python (in progress)")
        if (_noun_gap(text[m.end:s]) if status.group("noun") else _post_gap(text, m.end, s, degrees)):
            mark_chain(nearest)

    designations = resources.designations
    out: list[Mention] = []
    for i, mention in enumerate(mentions):
        if i not in marked or mention.canonical_id in designations:
            out.append(mention)
            continue
        designation = resources.in_progress.get(mention.canonical_id)
        if designation is not None:
            out.append(mention.model_copy(update={"canonical_id": designation}))
    return tuple(out)


class _StatusResolver:
    """A resolver whose `extract` reads credential status; everything else is the resolver's."""

    def __init__(self, resolver: Resolver, resources: ResumeResources) -> None:
        self._resolver = resolver
        self._resources = resources

    def extract(self, text: str, *, context: Literal["prose", "list"] = "prose",
                include_fields: bool = False) -> tuple[Mention, ...]:
        found = self._resolver.extract(text, context=context, include_fields=include_fields)
        return credential_status(text, found, self._resources)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolver, name)


# ─── places and work authorisation ──────────────────────────────────────────────────────────

_REGION_CODES: dict[str, str] = {
    **{code: "CA" for code in _entries._CA_CODES.split()},
    **{code: "US" for code in _entries._US_CODES.split()},
}
_REGION_NAME_CODES: dict[str, str] = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "newfoundland": "NL", "nova scotia": "NS", "northwest territories": "NT",
    "nunavut": "NU", "ontario": "ON", "prince edward island": "PE", "quebec": "QC", "saskatchewan": "SK",
    "yukon": "YT", "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
_CA_REGION_NAMES = frozenset(name for name, code in _REGION_NAME_CODES.items()
                             if _REGION_CODES.get(code) == "CA" and name != "california")
_COUNTRY_CODES: dict[str, str] = {
    "canada": "CA", "usa": "US", "us": "US", "united states": "US", "united states of america": "US",
    "uk": "GB", "united kingdom": "GB", "england": "GB", "france": "FR", "germany": "DE", "india": "IN",
    "china": "CN", "mexico": "MX", "ireland": "IE", "netherlands": "NL", "singapore": "SG", "japan": "JP",
    "australia": "AU", "brazil": "BR", "spain": "ES", "italy": "IT", "switzerland": "CH", "sweden": "SE",
    "israel": "IL", "belgium": "BE", "portugal": "PT", "poland": "PL", "nigeria": "NG", "pakistan": "PK",
    "philippines": "PH", "vietnam": "VN", "south korea": "KR", "korea": "KR",
}
_CA_CITY_PATTERN = _entries._CITIES.split("|New York")[0]
_CA_CITY = re.compile(rf"(?i:{_CA_CITY_PATTERN})")
_CITY_REGION = re.compile(rf"(?P<city>{_entries._CITY})\s*,\s*(?P<region>{_entries._REGION})"
                          rf"(?:\s*,\s*(?P<country>{_entries._COUNTRY}))?")
_CITY_COUNTRY = re.compile(rf"(?P<city>{_entries._CITY})\s*,\s*(?P<country>{_entries._COUNTRY})")
_KNOWN_CITY = re.compile(rf"(?P<city>(?i:{_entries._CITIES}))(?:\s*,\s*(?P<country>{_entries._COUNTRY}))?")
_REGION_ALONE = re.compile(rf"(?P<region>{_entries._REGION})(?:\s*,\s*(?P<country>{_entries._COUNTRY}))?")
_COUNTRY_ALONE = re.compile(rf"(?P<country>{_entries._COUNTRY})")
_PIECE_BREAK = re.compile(r"\s*(?:[|•·◦▪]|\t|\s{2,}|\s[-–—]\s)\s*")
_CONTACT_INFO = re.compile(r"@|https?:|www\.|\.(?:com|ca|io|dev|me|org|net)\b|linkedin|github", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\d)")


_MIN_LANGUAGE_LINES = 3  # an unsupported majority needs this many lines (a single résumé line misreads)


def _text_language(lines: Iterable[str]) -> Optional[str]:
    """The majority language of the lines (`detect_line_language`), or None with no signal. An
    unsupported language decides only with `_MIN_LANGUAGE_LINES` lines or more; below that the
    most frequent supported language (or None) stands, since one short line ("Data Analyst Co-op,
    Ontario Ministry of Finance") can read as Dutch. Distinct lines count once: a repeated line (a
    page header) is no more evidence."""
    distinct = dict.fromkeys(line.strip() for line in lines if line.strip())
    counts = Counter(code for code in (detect_line_language(line) for line in distinct) if code)
    if not counts:
        return None
    best = max(sorted(counts), key=lambda code: counts[code])
    if best in SUPPORTED_LANGUAGES or counts[best] >= _MIN_LANGUAGE_LINES:
        return best
    supported = [code for code in sorted(counts) if code in SUPPORTED_LANGUAGES]
    return max(supported, key=lambda code: counts[code]) if supported else None


def _key(text: str) -> str:
    return " ".join(fold(text).replace(".", "").split())


def _country(text: Optional[str]) -> Optional[str]:
    return _COUNTRY_CODES.get(_key(text)) if text else None


def _region(text: str) -> tuple[Optional[str], Optional[str]]:
    """(region code, country) of a region code or name."""
    stripped = text.strip()
    if stripped in _REGION_CODES:
        return stripped, _REGION_CODES[stripped]
    code = _REGION_NAME_CODES.get(_key(stripped))
    return (code, _REGION_CODES[code]) if code else (None, None)


def _place(piece: str) -> Optional[Location]:
    """The place a contact piece names (see the module docstring), else None."""
    found = _CITY_REGION.search(piece)
    if found is not None:
        city = found.group("city")
        region, country = _region(found.group("region"))
        stated = _country(found.group("country"))
        if region == "CA" and country == "US" and _CA_CITY.fullmatch(city):
            region, country = None, "CA"  # "Toronto, CA": Canada, not California
        elif stated and stated != country:
            region, country = None, stated
        return Location(country=country, region=region, city=city)
    found = _CITY_COUNTRY.search(piece)
    if found is not None and _country(found.group("country")):
        return Location(country=_country(found.group("country")), city=found.group("city"))
    found = _KNOWN_CITY.fullmatch(piece)
    if found is not None:
        return Location(country=_country(found.group("country")), city=found.group("city"))
    found = _REGION_ALONE.fullmatch(piece)
    if found is not None:
        region, country = _region(found.group("region"))
        if region is not None:
            return Location(country=_country(found.group("country")) or country, region=region)
    found = _COUNTRY_ALONE.fullmatch(piece)
    if found is not None and _country(found.group("country")):
        return Location(country=_country(found.group("country")))
    return None


def _pieces(line: str) -> list[str]:
    return [piece.strip() for piece in _PIECE_BREAK.split(line) if piece and piece.strip()]


def _is_contact_info(piece: str) -> bool:
    return _CONTACT_INFO.search(piece) is not None or _PHONE.search(piece) is not None


def _locations(lines: Iterable[str]) -> list[Location]:
    out: list[Location] = []
    for line in lines:
        for piece in _pieces(line):
            if "@" in piece or "http" in piece.casefold():
                continue
            place = _place(piece)
            if place is not None and place not in out:
                out.append(place)
    return out


_CITIZEN = re.compile(
    r"(?<![\w])(?:(?P<adj>canadian|american|u\.?\s?s\.?(?:a\.?)?|united\s+states)\s+citizen(?:ship)?|"
    r"citizen(?:ship)?\s+of\s+(?:the\s+)?(?P<of>canada|u\.?s\.?(?:a\.?)?|united\s+states(?:\s+of\s+america)?)|"
    r"(?P<fr>citoyen(?:ne)?\s+canadien(?:ne)?))(?![\w])", re.IGNORECASE)
_RESIDENT = re.compile(
    r"(?<![\w])(?:(?:(?P<adj>canadian|american|u\.?\s?s\.?)\s+)?permanent\s+resid(?:ent|ency)"
    r"(?:\s+(?:of|in)\s+(?:the\s+)?(?P<of>canada|u\.?s\.?(?:a\.?)?|united\s+states))?|"
    r"(?P<card>green\s+card)|(?P<fr>r[ée]sident(?:e)?\s+permanent(?:e)?))(?![\w])", re.IGNORECASE)
_PERMIT = re.compile(r"(?<![\w])(?:(?:open|post[-\s]?graduat(?:e|ion))\s+work\s+permit|pgwp|"
                     r"permis\s+de\s+travail\s+ouvert)(?![\w])", re.IGNORECASE)
_AUTHORIZED = re.compile(r"(?<![\w])(?:authori[sz]ed|eligible|legally\s+(?:able|entitled|permitted)|permitted|"
                         r"entitled)\s+to\s+work\s+in\s+(?P<places>[^.;|\n]{1,60})", re.IGNORECASE)
_NOT_BEFORE = re.compile(r"\bnot\s*$", re.IGNORECASE)
_NO_SPONSORSHIP = re.compile(
    r"(?<![\w])(?:(?:do(?:es)?\s+not|don'?t|doesn'?t|will\s+not|won'?t|would\s+not|never|no\s+longer)\s+"
    r"(?:require|need)\w*\s+(?:any\s+)?(?:visa\s+|work\s+|employer\s+|h-?1b\s+)?sponsorship|"
    r"no\s+(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:required|needed)|"
    r"sponsorship\s+(?:is\s+)?not\s+(?:required|needed)|without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship)",
    re.IGNORECASE)
_SPONSORSHIP = re.compile(r"(?<![\w])(?:require|need)s?\s+(?:visa\s+|work\s+|employer\s+|h-?1b\s+)?sponsorship",
                          re.IGNORECASE)


def _countries_named(text: str) -> list[str]:
    folded = fold(text)
    out = []
    if "canad" in folded:
        out.append("CA")
    if re.search(r"\bu\.?\s?s\.?a?\b|united\s+states|\bamerica", folded):
        out.append("US")
    return out


def _work_auth(lines: Sequence[str], locations: Sequence[Location]) -> Optional[WorkAuth]:
    text = "\n".join(lines)
    authorized: set[str] = set()
    citizenships: set[str] = set()
    for found in _CITIZEN.finditer(text):
        code = "CA" if found.group("fr") or "canad" in fold(found.group()) else "US"
        citizenships.add(code)
        authorized.add(code)
    contact_countries = {place.country for place in locations if place.country}
    for found in _RESIDENT.finditer(text):
        if found.group("fr") or "canad" in fold(found.group()):
            authorized.add("CA")
        elif found.group("card") or found.group("adj") or found.group("of"):
            authorized.add("US")
        elif len(contact_countries) == 1:
            authorized.update(contact_countries)
    if _PERMIT.search(text):
        authorized.add("CA")
    for found in _AUTHORIZED.finditer(text):
        if _NOT_BEFORE.search(text[: found.start()]):
            continue
        authorized.update(_countries_named(found.group("places")))
    negative = [match.span() for match in _NO_SPONSORSHIP.finditer(text)]
    positive = [match for match in _SPONSORSHIP.finditer(text)
                if not any(start <= match.start() < end for start, end in negative)]
    sponsorship: Optional[bool] = True if positive else (False if negative else None)
    if not authorized and not citizenships and sponsorship is None:
        return None
    return WorkAuth(authorized_countries=tuple(sorted(authorized)), citizenships=tuple(sorted(citizenships)),
                    needs_sponsorship=sponsorship)


# ─── text helpers ───────────────────────────────────────────────────────────────────────────

_BULLET = re.compile(r"^\s*(?:[•▪◦●‣∙➢➤►▶✓✔○■◆·–—]\s*|[-*]\s+)")
_LABEL = re.compile(r"^\s*(?:[-*•]\s*)?(?P<label>[^\W\d_][^:]{0,40}?)\s*:\s*")
_ITEM_BREAK = re.compile(r"[,;|•·/]|\s+(?:and|&|et)\s+")
_COURSE_LABEL = re.compile(r"course|cours", re.IGNORECASE)
_NAME_FORBIDDEN = re.compile(r"[\d@,|:;/()]")


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _unbulleted(line: str) -> str:
    return _BULLET.sub("", line, count=1).strip()


def _is_bullet(line: str) -> bool:
    return _BULLET.match(line) is not None and bool(_unbulleted(line))


def _label(line: str) -> tuple[str, int]:
    """("label", offset where the content starts) of a "Label: content" line; ("", 0) otherwise."""
    found = _LABEL.match(line)
    if found is None or len(found.group("label").split()) > 4 or not line[found.end():].strip():
        return "", 0
    return found.group("label"), found.end()


def _name_shaped(line: str) -> bool:
    tokens = line.split()
    return 1 <= len(tokens) <= 5 and _NAME_FORBIDDEN.search(line) is None and all(
        token[:1].isalpha() and token[:1].isupper() for token in tokens)


def _paragraphs(lines: Sequence[str]) -> list[str]:
    """Bullets one each; other consecutive lines joined into one paragraph (wrapped prose)."""
    out: list[str] = []
    joining = False
    for line in lines:
        text = line.strip()
        if not text:
            joining = False
            continue
        if _is_bullet(text):
            out.append(_unbulleted(text))
            joining = False
        elif joining and out:
            out[-1] = f"{out[-1]} {text}"
        else:
            out.append(text)
            joining = True
    return out


def _nonblank(lines: Iterable[str]) -> int:
    """Lines with text: a rule of dashes or equals signs under a header is decoration, not content
    (a plain-text rendering would otherwise read as less functional than its PDF, Task 5.3)."""
    return sum(1 for line in lines if any(ch.isalnum() for ch in line))


# ─── reference building ─────────────────────────────────────────────────────────────────────


def _surface(text: str) -> str:
    return text[:_MAX_TEXT]


def _unique(refs: Iterable[SkillRef], limit: int) -> tuple[SkillRef, ...]:
    """Once per (id, evidence, verb, polarity, source), in document order, within `limit`."""
    seen: set[tuple[str, Evidence, VerbClass, Polarity, SkillSource]] = set()
    out: list[SkillRef] = []
    for ref in refs:
        key = (ref.canonical_id, ref.evidence, ref.verb_class, ref.polarity, ref.source)
        if key not in seen:
            seen.add(key)
            out.append(ref)
            if len(out) >= limit:
                break
    return tuple(out)


class _Reader:
    """Mentions into `SkillRef`s, with the credential-status resolver."""

    def __init__(self, resolver: Resolver, resources: ResumeResources, config: EngineConfig) -> None:
        self.resolver = _StatusResolver(resolver, resources)
        self.raw_resolver = resolver
        self.resources = resources
        self.config = config

    def _analysed(self, text: str, mentions: Sequence[Mention]):
        bullets = [BulletMention(canonical_id=m.canonical_id, start=m.start, end=m.end) for m in mentions]
        return analyze_bullet(text, bullets, self.config)

    def blob(self, text: str, source: SkillSource, *, context: Literal["prose", "list"] = "prose",
             skip_before: int = 0) -> list[SkillRef]:
        """SKILLS_BLOB refs with the polarity of `analyze_bullet` (a mention ending by
        `skip_before`, inside a list label such as "Tools:", is not read)."""
        mentions = [m for m in self.resolver.extract(text, context=context) if m.end > skip_before]
        if not mentions:
            return []
        return [SkillRef(canonical_id=m.canonical_id, surface_form=_surface(m.surface), evidence=Evidence.SKILLS_BLOB,
                         polarity=a.polarity, source=source)
                for m, a in zip(mentions, self._analysed(text, mentions))]

    def bullet(self, text: str, source: SkillSource) -> list[SkillRef]:
        """ROLE_BULLET[_QUANTIFIED] refs with their verb class and polarity (undated)."""
        mentions = self.resolver.extract(text)
        if not mentions:
            return []
        evidence = Evidence.ROLE_BULLET_QUANTIFIED if is_quantified(text, self.config) else Evidence.ROLE_BULLET
        return [SkillRef(canonical_id=m.canonical_id, surface_form=_surface(m.surface), evidence=evidence,
                         verb_class=a.verb_class, polarity=a.polarity, source=source)
                for m, a in zip(mentions, self._analysed(text, mentions))]

    def coursework(self, text: str, skip_before: int = 0) -> list[SkillRef]:
        """Course names: ROLE_BULLET of verb EXPOSURE, source EDUCATION (SPEC §15.1)."""
        return [SkillRef(canonical_id=m.canonical_id, surface_form=_surface(m.surface), evidence=Evidence.ROLE_BULLET,
                         verb_class=VerbClass.EXPOSURE, source=SkillSource.EDUCATION)
                for m in self.resolver.extract(text, context="list") if m.end > skip_before]

    def credentials(self, text: str) -> tuple[list[SkillRef], list[SkillRef]]:
        """(CERTIFICATION refs for credential nodes, SKILLS_BLOB refs for the other mentions)."""
        held: list[SkillRef] = []
        other: list[SkillRef] = []
        for m in self.resolver.extract(text, context="list"):
            if m.canonical_id in self.resources.credentials:
                held.append(SkillRef(canonical_id=m.canonical_id, surface_form=_surface(m.surface),
                                     evidence=Evidence.CERTIFICATION, source=SkillSource.CERTIFICATION))
            else:
                other.append(SkillRef(canonical_id=m.canonical_id, surface_form=_surface(m.surface),
                                      evidence=Evidence.SKILLS_BLOB, source=SkillSource.CERTIFICATION))
        return held, other

    def unresolved(self, text: str, start: int = 0) -> list[str]:
        """List items of `text[start:]` that hold no mention."""
        mentions = self.raw_resolver.extract(text, context="list")
        out: list[str] = []
        cuts = [start] + [m.end() for m in _ITEM_BREAK.finditer(text, start)] + [len(text) + 1]
        for k in range(len(cuts) - 1):
            left, right = cuts[k], cuts[k + 1]
            item = text[left:right].strip(" \t,;|•·/&-–—.:()[]")
            item = re.sub(r"^(?:and|et)\s+|\s+(?:and|et|&)$", "", item, flags=re.IGNORECASE).strip()
            if not item or not any(ch.isalpha() for ch in item) or len(item.split()) > 6:
                continue
            if any(m.start < right and m.end > left for m in mentions):
                continue
            out.append(" ".join(item.casefold().split())[:_MAX_TOKEN])
        return out


# ─── the assembly ───────────────────────────────────────────────────────────────────────────


class _Assembly:
    """Collects what the sections give; `resume` builds the `Resume`."""

    def __init__(self, *, as_of: dt.date, reader: _Reader, parse_config: ParseConfig, lang: str) -> None:
        self.as_of = as_of
        self.reader = reader
        self.parse_config = parse_config
        self.lang = lang
        self.roles: list[Role] = []
        self.scope: list[ScopeFeatures] = []
        self.projects: list[Project] = []
        self.education: list[Education] = []
        self.certs: list[SkillRef] = []
        self.blob: list[SkillRef] = []
        self.unresolved: list[str] = []
        self.unmapped: list[str] = []
        self.locations: list[Location] = []
        self.auth_lines: list[str] = []
        self.flags: set[Flag] = set()
        self.listed = False  # skills or summary content (FUNCTIONAL_RESUME)

    # roles ---------------------------------------------------------------------------------

    def add_role(self, draft: RoleDraft) -> None:
        if len(self.roles) >= _MAX_ROLES:
            self.flags.add(Flag.TRUNCATED)
            return
        if draft.date_range is not None:
            self.flags.update(draft.date_range.flags)
        resources = self.reader.resources
        role = build_role(draft, as_of=self.as_of, resolver=self.reader.resolver, titles=resources.titles,
                          mos=resources.mos, parse_config=self.parse_config, config=self.reader.config)
        self.roles.append(role)
        self.scope.append(extract_scope(draft.bullets))
        if role.title_raw and role.title_canonical is None:
            self.unmapped.append(role.title_raw)

    # projects ------------------------------------------------------------------------------

    def add_project(self, name: str, date_range: Optional[DateRange], bullets: Sequence[str]) -> None:
        if len(self.projects) >= _MAX_PROJECTS:
            self.flags.add(Flag.TRUNCATED)
            return
        refs: list[SkillRef] = []
        for text in (name, *bullets):
            text = " ".join(text.split())
            if text:
                refs.extend(self.reader.bullet(text, SkillSource.PROJECT))
        start = end = None
        if date_range is not None:
            self.flags.update(date_range.flags)
            if date_range.start is not None:
                start = date_range.start
                end = date_range.end if not date_range.is_current else DateValue.exact(self.as_of)
                if end is not None and end.value < start.value:
                    end = start  # an as_of before the project's start: keep the project valid
        self.projects.append(Project(name=_surface(" ".join(name.split())), skills=_unique(refs, _MAX_SKILLS),
                                     start=start, end=end))

    # skills --------------------------------------------------------------------------------

    def add_skill_line(self, line: str, source: SkillSource = SkillSource.SKILLS_SECTION) -> None:
        text = _unbulleted(line)
        if not text:
            return
        self.listed = True
        _, content = _label(text)
        self.blob.extend(self.reader.blob(text, source, context="list", skip_before=content))
        self.unresolved.extend(self.reader.unresolved(text, content))

    def add_prose(self, lines: Sequence[str], source: SkillSource) -> None:
        for paragraph in _paragraphs(lines):
            self.blob.extend(self.reader.blob(paragraph, source))

    def add_certification_entries(self, lines: Sequence[str]) -> None:
        entries: list[list[str]] = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            bullet = _is_bullet(text)
            text = _unbulleted(text)
            if entries and (bullet or not self.reader.resolver.extract(text, context="list")):
                entries[-1].append(text)
            else:
                entries.append([text])
        for entry in entries:
            self.add_credentials("\n".join(entry))

    def add_credentials(self, text: str) -> None:
        held, other = self.reader.credentials(text)
        self.certs.extend(held)
        self.blob.extend(other)

    def add_coursework(self, text: str) -> None:
        text = _unbulleted(text)
        _, content = _label(text)
        self.blob.extend(self.reader.coursework(text, content))

    # education -----------------------------------------------------------------------------

    def add_education_section(self, section: Section) -> None:
        for entry in parse_education(section, resolver=self.reader.raw_resolver, parse_config=self.parse_config,
                                     as_of=self.as_of):
            if entry not in self.education and len(self.education) < _MAX_EDUCATION:
                self.education.append(entry)
        in_courses = False
        previous = ""
        for line in section.lines:
            text = _unbulleted(line)
            if not text:
                in_courses = False
                continue
            label, _ = _label(text)
            if label and _COURSE_LABEL.search(label):
                in_courses = True
            elif in_courses and not (previous.rstrip().endswith((",", ";", "&")) or text[:1].islower()):
                in_courses = False
            previous = text
            if in_courses:
                self.add_coursework(text)
            elif not label or not _COURSE_LABEL.search(label):
                if not degree_matches(text, self.parse_config) and not find_date_ranges(text, as_of=self.as_of):
                    self.blob.extend(self.reader.blob(text, SkillSource.EDUCATION))

    # contact -------------------------------------------------------------------------------

    def add_contact(self, lines: Sequence[str], name: str) -> None:
        self.locations.extend(place for place in _locations(lines) if place not in self.locations)
        for line in lines:
            if not line.strip() or (name and line.strip() == name):
                continue
            kept = [piece for piece in _pieces(line) if not _is_contact_info(piece) and _place(piece) is None]
            if kept:
                self.blob.extend(self.reader.blob(" | ".join(kept), SkillSource.OTHER))

    # result --------------------------------------------------------------------------------

    def resume(self, *, tier: ParseTier, flags: set[Flag], functional: bool) -> Resume:
        dated = [role for role in self.roles if role.dated]
        if functional:
            flags.add(Flag.FUNCTIONAL_RESUME)
        return Resume(
            roles=tuple(self.roles),
            projects=tuple(self.projects),
            education=tuple(self.education[:_MAX_EDUCATION]),
            certifications=_unique(self.certs, _MAX_CERTS),
            skills_blob=_unique(self.blob, _MAX_BLOB),
            total_experience_months=_union_months(dated, self.as_of),
            locations=tuple(self.locations[:_MAX_LOCATIONS]),
            work_auth=_work_auth(self.auth_lines, self.locations),
            parse_tier=tier,
            language=self.lang,
            flags=_ordered(flags),
        )

    def functional(self, *, qualification_lines: int, body_lines: int) -> bool:
        if any(role.dated for role in self.roles):
            return False
        if self.roles and self.listed:
            return True
        return qualification_lines > 0 and 2 * qualification_lines >= body_lines

    def parsed(self, *, tier: ParseTier, flags: Iterable[Flag], sections: Sequence[SectionKind],
               qualification_lines: int, body_lines: int) -> ParsedResume:
        all_flags = set(flags) | self.flags
        functional = self.functional(qualification_lines=qualification_lines, body_lines=body_lines)
        resume = self.resume(tier=tier, flags=all_flags, functional=functional)
        return ParsedResume(
            resume=resume,
            flags=resume.flags,
            unresolved_tokens=tuple(dict.fromkeys(self.unresolved))[:_MAX_TOKENS],
            unmapped_titles=tuple(dict.fromkeys(self.unmapped)),
            scope=tuple(self.scope),
            sections=tuple(sections),
            parse_config_version=self.parse_config.version,
        )


def _union_months(roles: Sequence[Role], as_of: dt.date) -> Optional[float]:
    """Months of the union of the dated roles' intervals (overlaps counted once)."""
    intervals = []
    for role in roles:
        assert role.start is not None
        end = as_of if role.is_current else role.end.value  # type: ignore[union-attr]
        if end > role.start.value:
            intervals.append((role.start.value, end))
    if not roles:
        return None
    total = 0.0
    current: Optional[tuple[dt.date, dt.date]] = None
    for start, end in sorted(intervals):
        if current is None or start > current[1]:
            if current is not None:
                total += months_between(*current)
            current = (start, end)
        else:
            current = (current[0], max(current[1], end))
    if current is not None:
        total += months_between(*current)
    return min(_MAX_MONTHS, max(0.0, total))


# ─── the text path ──────────────────────────────────────────────────────────────────────────


def _truncate(sections: Sequence[Section], cap: int) -> tuple[list[Section], bool]:
    """Experience-like sections cut after `cap` whitespace tokens, in document order."""
    budget = cap
    out: list[Section] = []
    cut = False
    for section in sections:
        if section.kind in _EXPERIENCE_LIKE:
            kept: list[str] = []
            for line in section.lines:
                n = len(line.split())
                if n > budget:
                    cut = True
                    break
                budget -= n
                kept.append(line)
            if len(kept) < len(section.lines):
                section = section.model_copy(update={"lines": tuple(kept)})
        out.append(section)
    return out, cut


def _project_entries(lines: Sequence[str]) -> list[tuple[list[str], list[str]]]:
    """(name lines, bullets) per project (see the module docstring)."""
    out: list[tuple[list[str], list[str]]] = []
    current: Optional[tuple[list[str], list[str]]] = None
    for line in lines:
        text = line.strip()
        if not text:
            current = None
            continue
        if _is_bullet(text):
            if current is None:
                current = ([], [])
                out.append(current)
            current[1].append(_unbulleted(text))
            continue
        if current is not None and current[1]:
            previous = current[1][-1]
            probe = _entries.Line(0, text, text, False, False, False, False, False, False, False)
            if _entries.continues(previous, probe):
                current[1][-1] = f"{previous} {text}"
                continue
            current = ([text], [])
            out.append(current)
        elif current is not None:
            if _entries.is_sentence(text):
                current[1].append(text)
            else:
                current[0].append(text)
        else:
            current = ([text], [])
            out.append(current)
    return out


_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
_HEADER_PIECE = re.compile(r"\s*[|•·]\s*")


def _split_header(header: str, as_of: dt.date, lang: str) -> tuple[str, Optional[DateRange]]:
    """(name, first date range) of a project header line."""
    found = [item for item in find_date_ranges(header, as_of=as_of, lang="fr" if lang == "fr" else "en")
             if item[2].start is not None or item[2].flags]
    if not found:
        return header.strip(), None
    start, end, date_range = found[0]
    rest = header[:start] + header[end:]
    pieces = []
    for piece in _HEADER_PIECE.split(rest):
        piece = _EMPTY_BRACKETS.sub("", piece).strip(" ,;:-–—")
        if piece:
            pieces.append(piece)
    return " | ".join(pieces), date_range


def _section_language(section: Section, default: str) -> str:
    return "fr" if section.language == "fr" else default


def parse_resume_text(text: str, *, as_of: dt.date, resolver: Resolver, config: EngineConfig,
                      parse_config: ParseConfig, lines: Sequence[TextLine] | None = None,
                      tier: ParseTier = ParseTier.A, language: str | None = None, flags: Sequence[Flag] = (),
                      resources: ResumeResources | None = None, strict: bool = False) -> ParsedResume:
    """A résumé from its text (see the module docstring). `lines` are ingest's lines (their layout
    helps find styled headers); `tier`, `language` and `flags` come from ingest too. `resources`
    default to the taxonomy in `data/taxonomy`."""
    try:
        return _parse_text(text, as_of=as_of, resolver=resolver, config=config, parse_config=parse_config,
                           lines=lines, tier=tier, language=language, flags=flags, resources=resources)
    except Exception:
        if strict:
            raise
        _logger.warning("parse_resume_text: internal error", exc_info=True)
        return _failed(Flag.INTERNAL_ERROR, parse_config)


def _parse_text(text: Any, *, as_of: dt.date, resolver: Resolver, config: EngineConfig, parse_config: ParseConfig,
                lines: Optional[Sequence[TextLine]], tier: ParseTier, language: Optional[str],
                flags: Sequence[Flag], resources: Optional[ResumeResources]) -> ParsedResume:
    if not isinstance(text, str) or not isinstance(as_of, dt.date):
        return _failed(Flag.INVALID_INPUT, parse_config)
    given = [flag for flag in flags if isinstance(flag, Flag)]
    if lines:
        raw = [line.text if isinstance(line, TextLine) else str(line) for line in lines]
        layout = [line for line in lines if isinstance(line, TextLine)]
        layout = layout if len(layout) == len(raw) else None
    else:
        raw = normalize_text(text).split("\n")
        layout = None
    if not any(line.strip() for line in raw):
        return _failed(Flag.NOT_A_RESUME, parse_config, given)
    lang = language if isinstance(language, str) and language else _text_language(raw)
    if lang is not None and lang not in SUPPORTED_LANGUAGES:
        return _failed(Flag.UNSUPPORTED_LANGUAGE, parse_config, given)
    lang = lang or "en"
    resources = resources if resources is not None else load_resume_resources(parse_config=parse_config)
    reader = _Reader(resolver, resources, config)
    assembly = _Assembly(as_of=as_of, reader=reader, parse_config=parse_config, lang=lang)

    sections, segment_flags = segment(raw, parse_config=parse_config, layout=layout)
    kinds = tuple(section.kind for section in sections)
    sections, truncated = _truncate(sections, config.scoring.ingest.max_experience_tokens)
    if truncated:
        assembly.flags.add(Flag.TRUNCATED)
    first = next((line.strip() for line in raw if line.strip()), "")
    name = first if _name_shaped(first) and not resolver.extract(first) else ""
    no_sections = Flag.NO_SECTIONS in segment_flags

    qualification_lines = body_lines = 0
    for position, section in enumerate(sections):
        kind = section.kind
        count = _nonblank(section.lines)
        if kind is not SectionKind.CONTACT:
            body_lines += count
        if kind in (SectionKind.SUMMARY, SectionKind.SKILLS) and any(
                word in header_key(section.header) for word in _QUALIFICATION_WORDS):
            qualification_lines += count
        if kind not in _NO_WORK_AUTH:
            assembly.auth_lines.extend(section.lines)
        _read_section(assembly, section, name=name if position == 0 else "",
                      contact=kind is SectionKind.CONTACT or (no_sections and position == 0))
    return assembly.parsed(tier=tier if isinstance(tier, ParseTier) else ParseTier.A,
                           flags=[*given, *segment_flags], sections=kinds,
                           qualification_lines=qualification_lines, body_lines=body_lines)


def _read_section(assembly: _Assembly, section: Section, *, name: str, contact: bool) -> None:
    kind = section.kind
    as_of = assembly.as_of
    if kind in _EXPERIENCE_LIKE:
        drafts = split_roles(section, as_of=as_of, lang=_section_language(section, assembly.lang))
        for draft in drafts:
            assembly.add_role(draft)
        if not drafts:  # no entry structure: its text is skills-blob evidence, never a role
            assembly.add_prose(section.lines, SkillSource.OTHER)
    elif kind is SectionKind.PROJECTS:
        lang = _section_language(section, assembly.lang)
        for name_lines, bullets in _project_entries(section.lines):
            header, date_range = _split_header(" | ".join(name_lines), as_of, lang)
            assembly.add_project(header, date_range, bullets)
    elif kind in (SectionKind.SKILLS, SectionKind.LANGUAGES):
        for line in section.lines:
            assembly.add_skill_line(line)
    elif kind is SectionKind.CERTIFICATIONS:
        assembly.add_certification_entries(section.lines)
    elif kind is SectionKind.COURSEWORK:
        for line in section.lines:
            if line.strip():
                assembly.add_coursework(line)
    elif kind is SectionKind.EDUCATION:
        assembly.add_education_section(section)
    elif kind in _NO_SKILLS:
        return
    elif contact:
        lines = list(section.lines)
        if kind is not SectionKind.CONTACT:  # no headers: the first lines are the contact block
            head = [line for line in lines if line.strip()][:4]
            assembly.locations.extend(place for place in _locations(head) if place not in assembly.locations)
            assembly.add_prose([line for line in lines if not name or line.strip() != name], SkillSource.OTHER)
        else:
            assembly.add_contact(lines, name)
    else:
        source = SkillSource.SUMMARY if kind is SectionKind.SUMMARY else SkillSource.OTHER
        if kind is SectionKind.SUMMARY and _nonblank(section.lines):
            assembly.listed = True
        assembly.add_prose([line for line in section.lines if not name or line.strip() != name], source)


# ─── files ──────────────────────────────────────────────────────────────────────────────────


def parse_resume_bytes(data: bytes, filename: str, *, as_of: dt.date, resolver: Resolver, config: EngineConfig,
                       parse_config: ParseConfig, resources: ResumeResources | None = None,
                       strict: bool = False) -> ParsedResume:
    """`ingest_bytes`, then `parse_resume_text` with ingest's lines, tier, language and flags. An
    ingest failure gives its reasons (and its flags) and no résumé."""
    ingested = ingest_bytes(data, filename, config=config)
    if not ingested.ok:
        return ParsedResume(resume=None, reasons=ingested.reasons, flags=ingested.flags,
                            parse_config_version=getattr(parse_config, "version", ""))
    return parse_resume_text(ingested.text, as_of=as_of, resolver=resolver, config=config,
                             parse_config=parse_config, lines=ingested.lines, tier=ingested.tier,
                             language=ingested.language, flags=ingested.flags, resources=resources, strict=strict)


# ─── Tailrd's stored document (D-80) ─────────────────────────────────────────────────────────


def _doc_range(start: Any, end: Any, as_of: dt.date) -> Optional[DateRange]:
    first, last = _str(start).strip(), _str(end).strip()
    if first and last:
        text = f"{first} – {last}"
    else:
        text = first or last
    return parse_range(text, as_of=as_of) if text else None


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _has_content(doc: Mapping[str, Any]) -> bool:
    for item in _items(doc.get("experience")):
        if any(_str(item.get(key)).strip() for key in ("title", "company")) or any(
                s.strip() for s in _strings(item.get("bullets"))):
            return True
    for item in _items(doc.get("education")):
        if any(_str(item.get(key)).strip() for key in ("degree", "school")):
            return True
    for item in _items(doc.get("projects")):
        if _str(item.get("name")).strip() or any(s.strip() for s in _strings(item.get("bullets"))):
            return True
    if any(s.strip() for s in _strings(doc.get("skills"))):
        return True
    technologies = doc.get("technologies")
    if isinstance(technologies, Mapping) and any(
            s.strip() for values in technologies.values() for s in _strings(values)):
        return True
    return False


def _has_other_text(doc: Mapping[str, Any]) -> bool:
    """A summary or a custom section with any text."""
    if _str(doc.get("summary")).strip():
        return True
    for section in _items(doc.get("custom_sections")):
        texts = [_str(section.get("text")), *_strings(section.get("bullets"))]
        for item in _items(section.get("items")):
            texts.extend(_str(value) for value in item.values())
            texts.extend(_strings(item.get("bullets")))
        if any(text.strip() for text in texts):
            return True
    return False


def _doc_text(doc: Mapping[str, Any]) -> str:
    """Every text of the document, for its language."""
    parts: list[str] = [_str(doc.get("summary"))]
    for key in ("experience", "education", "projects"):
        for item in _items(doc.get(key)):
            parts.extend(_str(value) for value in item.values())
            parts.extend(_strings(item.get("bullets")) + _strings(item.get("achievements")))
    for section in _items(doc.get("custom_sections")):
        parts.append(_str(section.get("text")))
        parts.extend(_strings(section.get("bullets")))
    return "\n".join(part for part in parts if part.strip())


def resume_from_document(doc: Mapping[str, Any], *, as_of: dt.date, resolver: Resolver, config: EngineConfig,
                         parse_config: ParseConfig, raw_text: str | None = None,
                         resources: ResumeResources | None = None, strict: bool = False) -> ParsedResume:
    """A résumé from Tailrd's stored résumé document (D-80; see the module docstring)."""
    try:
        if not isinstance(doc, Mapping) or not isinstance(as_of, dt.date):
            return _failed(Flag.INVALID_INPUT, parse_config)
        if not _has_content(doc) and isinstance(raw_text, str) and raw_text.strip():
            return parse_resume_text(raw_text, as_of=as_of, resolver=resolver, config=config,
                                     parse_config=parse_config, resources=resources, strict=strict)
        if not _has_content(doc) and not _has_other_text(doc):
            return _failed(Flag.NOT_A_RESUME, parse_config)
        return _from_document(doc, as_of=as_of, resolver=resolver, config=config, parse_config=parse_config,
                              resources=resources)
    except Exception:
        if strict:
            raise
        _logger.warning("resume_from_document: internal error", exc_info=True)
        return _failed(Flag.INTERNAL_ERROR, parse_config)


def _from_document(doc: Mapping[str, Any], *, as_of: dt.date, resolver: Resolver, config: EngineConfig,
                   parse_config: ParseConfig, resources: Optional[ResumeResources]) -> ParsedResume:
    lang = _text_language(normalize_text(_doc_text(doc)).splitlines())
    if lang is not None and lang not in SUPPORTED_LANGUAGES:
        return _failed(Flag.UNSUPPORTED_LANGUAGE, parse_config)
    lang = lang or "en"
    resources = resources if resources is not None else load_resume_resources(parse_config=parse_config)
    reader = _Reader(resolver, resources, config)
    assembly = _Assembly(as_of=as_of, reader=reader, parse_config=parse_config, lang=lang)
    kinds: list[SectionKind] = []

    def clean(value: Any) -> str:
        return normalize_text(_str(value)).strip()

    location = clean(doc.get("location"))
    if location:
        kinds.append(SectionKind.CONTACT)
        assembly.locations.extend(_locations([location]))

    summary = normalize_text(_str(doc.get("summary")))
    if summary.strip():
        kinds.append(SectionKind.SUMMARY)
        assembly.listed = True
        assembly.auth_lines.extend(summary.split("\n"))
        assembly.add_prose(summary.split("\n"), SkillSource.SUMMARY)

    experience = _items(doc.get("experience"))
    if experience:
        kinds.append(SectionKind.EXPERIENCE)
    for item in experience:
        title, company = clean(item.get("title")), clean(item.get("company"))
        bullets = tuple(text for text in (clean(b) for b in _strings(item.get("bullets"))) if text)
        if not (title or company or bullets):
            continue
        assembly.add_role(RoleDraft(title_raw=title, employer=company, location=clean(item.get("location")),
                                    date_range=_doc_range(item.get("start_date"), item.get("end_date"), as_of),
                                    bullets=bullets))

    education = _items(doc.get("education"))
    if education:
        kinds.append(SectionKind.EDUCATION)
    for item in education:
        school, degree = clean(item.get("school")), clean(item.get("degree"))
        dates = " – ".join(part for part in (clean(item.get("start_date")), clean(item.get("end_date"))) if part)
        if degree or school:
            # School and dates only let parse_education infer a level and read "in progress", as
            # in the text path; neither reaches the output (Education has no field for them).
            lines = [school, f"{degree} | {dates}" if degree and dates else (degree or dates)]
            section = Section(kind=SectionKind.EDUCATION, header="", lines=tuple(line for line in lines if line),
                              start_line=0, language=None)
            assembly.add_education_section(section)
        for course in _strings(item.get("coursework")):
            if clean(course):
                assembly.add_coursework(clean(course))
        for achievement in _strings(item.get("achievements")):
            if clean(achievement):
                assembly.blob.extend(reader.blob(clean(achievement), SkillSource.EDUCATION))

    projects = _items(doc.get("projects"))
    if projects:
        kinds.append(SectionKind.PROJECTS)
    for item in projects:
        name = clean(item.get("name"))
        bullets = [text for text in (clean(b) for b in _strings(item.get("bullets"))) if text]
        if name or bullets:
            assembly.add_project(name, _doc_range(item.get("start_date"), item.get("end_date"), as_of), bullets)

    listed = [clean(s) for s in _strings(doc.get("skills"))]
    technologies = doc.get("technologies")
    if isinstance(technologies, Mapping):
        listed += [clean(s) for values in technologies.values() for s in _strings(values)]
    listed = [s for s in listed if s]
    if listed:
        kinds.append(SectionKind.SKILLS)
    for skill in listed:
        assembly.add_skill_line(skill)

    for section in _items(doc.get("custom_sections")):
        _read_custom_section(assembly, section, kinds)

    return assembly.parsed(tier=ParseTier.A, flags=(), sections=kinds, qualification_lines=0, body_lines=1)


_CERTIFICATION_KINDS = ("certifications",)


def _read_custom_section(assembly: _Assembly, section: Mapping[str, Any], kinds: list[SectionKind]) -> None:
    title = normalize_text(_str(section.get("title"))).strip()
    kind = classify_header(title, assembly.parse_config) if title else None
    if _str(section.get("kind")) in _CERTIFICATION_KINDS:
        kind = SectionKind.CERTIFICATIONS
    kind = kind or SectionKind.OTHER
    kinds.append(kind)
    text = normalize_text(_str(section.get("text")))
    bullets = [normalize_text(b).strip() for b in _strings(section.get("bullets"))]
    entries: list[str] = []
    for item in _items(section.get("items")):
        dates = " – ".join(part for part in (_str(item.get("start_date")).strip(), _str(item.get("end_date")).strip())
                           if part)
        parts = [_str(item.get(key)).strip() for key in ("title", "subtitle", "detail")] + [dates]
        parts += [b.strip() for b in _strings(item.get("bullets"))]
        entry = normalize_text("\n".join(part for part in parts if part)).strip()
        if entry:
            entries.append(entry)
    lines = [line for line in text.split("\n") if line.strip()] + [b for b in bullets if b]
    if kind not in _NO_WORK_AUTH:
        assembly.auth_lines.extend(lines + entries)
    if kind in _EXPERIENCE_LIKE:  # "Volunteer Experience", "Leadership", "Research Experience"
        for item in _items(section.get("items")):
            _add_custom_role(assembly, item)
        assembly.add_prose(lines, SkillSource.OTHER)  # text outside items: never a role
        return
    if kind is SectionKind.CERTIFICATIONS:
        for entry in lines + entries:
            assembly.add_credentials(entry)
    elif kind in _NO_SKILLS:
        return
    elif kind is SectionKind.COURSEWORK:
        for entry in lines + entries:
            assembly.add_coursework(entry)
    elif kind in (SectionKind.SKILLS, SectionKind.LANGUAGES):
        for entry in lines + entries:
            assembly.add_skill_line(entry)
    else:
        source = SkillSource.SUMMARY if kind is SectionKind.SUMMARY else SkillSource.OTHER
        assembly.add_prose(lines, source)
        for entry in entries:
            assembly.add_prose(entry.split("\n"), source)


def _add_custom_role(assembly: _Assembly, item: Mapping[str, Any]) -> None:
    """One role per item of an experience-like custom section, as `build_role` reads a text entry:
    the title, the subtitle as employer, the dates, the detail and bullets as bullets."""
    def clean(value: Any) -> str:
        return normalize_text(_str(value)).strip()

    title, employer = clean(item.get("title")), clean(item.get("subtitle"))
    bullets = tuple(text for text in (clean(b) for b in [_str(item.get("detail")), *_strings(item.get("bullets"))])
                    if text)
    if title or employer or bullets:
        assembly.add_role(RoleDraft(title_raw=title, employer=employer, location=clean(item.get("location")),
                                    date_range=_doc_range(item.get("start_date"), item.get("end_date"), assembly.as_of),
                                    bullets=bullets))
