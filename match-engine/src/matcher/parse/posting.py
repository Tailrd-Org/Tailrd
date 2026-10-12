"""Job postings: description text -> `JobPosting` (SPEC §2, §4, §14.4, §16; Task 6.5).

`parse_posting(title, description, *, meta, as_of, resolver, graph, config, parse_config, titles)`
reads one posting deterministically, with no clock and no I/O. Precision comes first: a phantom
requirement lowers every candidate's score on the posting, so where a reading is uncertain the
softer one is taken (IMPLIED rather than REQUIRED, a warning gate rather than a failing one).

* Text. Stored descriptions may hold escaped HTML: entities are decoded (twice at most, for
  "&amp;lt;p&amp;gt;"), tags become line breaks ("<li>" a bullet) or vanish, script and style
  bodies are dropped, then the ingest normalisation runs (`normalize_text`). Raw markup never
  reaches the resolver. A description cut at a stored-length cap (`truncation.caps`, ending in
  "..." or "…") is read as far as it goes, its last (cut) word dropped, and flagged TRUNCATED:
  the cut is never taken for the end of a requirements list (no BOILERPLATE_ONLY on it).
* Sections (`posting.yaml` `sections`): a header line (or a "Label: text" line) opens REQUIRED
  (requirements, qualifications, what you'll need, must have, exigences, ...), PREFERRED
  (preferred, nice to have, bonus, pluses, assets, atouts, ...), RESPONSIBILITIES (IMPLIED), ABOUT
  or IGNORED (benefits, EEO, how to apply: no requirements). Text before any header, or under an
  unknown one, is prose. A label inside a bullet applies to that bullet only.
* Necessity per clause (modal phrases): a clause's own phrase decides (implied and negated phrases
  first: "willingness to learn Go" and "no Rust experience required" name no requirement; then
  preferred; then required). In a PREFERRED section a required phrase stays PREFERRED. A clause
  without a phrase takes the next clause's phrase when the clauses between are bare lists and that
  clause continues the list ("Java, Python or Go is required"), else the previous clause's when it
  is a bare list itself ("Must have: Python, SQL"), else the section's default (prose and
  responsibilities: IMPLIED). Every skill mention becomes a requirement of its clause's necessity;
  the same skill in several places keeps the strongest (REQUIRED > PREFERRED > IMPLIED).
* OR-groups (D-31): mentions joined only by list separators form runs. A run after a lead-in
  ("such as", "e.g.", "one of") is one group; a run "X, Y or Z" (commas, then "or") is one group;
  otherwise "X or Y" and "X/Y" pairs are. "X or equivalent/similar/other ..." flags the group (or a
  one-member group) `or_equivalent`. Groups sharing a member merge. A group one of whose members is
  also required on its own at least as strongly is redundant: its other members fall to IMPLIED
  (never left behind as a lone requirement the posting did not state). Labels are `or:<n>` in order
  of appearance, so they never collide with skill ids.
* Years: "3+ years of Python" sets that requirement's `min_years` ("Python (3+ years)" too); "3+
  years of experience", "minimum 2 years", "at least 5 years", "1-2 years" (-> 1) with no skill
  set `min_years_total`, from REQUIRED clauses only, the smallest stated. "18 years of age", "a
  4-year degree", "2-year contract" are not experience. A skill's `min_years` beyond
  `as_of.year - release_year` is clamped to that span and flagged UNREALISTIC_REQUIREMENT.
* Education: degree words in REQUIRED or PREFERRED clauses (bare "Master" only beside a degree
  word, a field or schooling: "Scrum Master" is none); alternatives take the lowest level
  ("Bachelor's or Master's"); "degree in <field>" alone is a bachelor's; fields are the clause's
  `field.*` ids; "or equivalent experience" sets `or_equivalent`; REQUIRED wins over PREFERRED
  ("Bachelor's required, Master's preferred"). A skill that is also the named field
  ("degree in Statistics") is the field there, not a skill requirement.
* Credentials (CREDENTIAL nodes) are never skill requirements. In a REQUIRED clause that shows
  the credential is to be held ("RN with 3+ years", "valid P.Eng. licence", "CPA designation
  required") they are `certifications_required` and a `LicenseGate` (alternatives in one gate:
  "P.Eng. or EIT"); in PREFERRED clauses `certifications_preferred`. "Work under RN supervision"
  is neither.
* Gates (§4, D-60..D-64), read from every sentence (EEO and footer text included) but never from
  a preferred one: work authorisation (country named, else none: the gate then falls back to the
  posting's places), sponsorship refused or offered (both stated: unknown), citizenship (always
  with countries: the one named, else the posting's; "citizen or permanent resident" and "U.S.
  person" are authorisation, not citizenship); security clearance as `<CC>_<LEVEL>` ladder tokens
  (a level tied to one country takes it; Secret/Top Secret take the country named, else the
  posting's, else no level), active only when the sentence says active/current/hold/possess (an
  "able to obtain" reading wins a tie), never in a negated sentence ("no clearance required");
  `meta.work_type == "onsite"` with places gives a `LocationGate`.
* Overflow (§14.4): more than `posting.max_weighted_requirements` REQUIRED/PREFERRED units (a
  group is one unit) keeps the top by IDF (ties by id); the rest become IMPLIED at
  `overflow_weight_multiplier`, flagged REQUIREMENTS_TRUNCATED.
* Flags: POSTING_UNSCORABLE (no requirement at all: an empty description is title and metadata
  only, never an invented requirement), BOILERPLATE_ONLY, CONTRADICTORY_REQUIREMENTS (an IC0/IC1
  posting requiring `contradiction.min_years`+; both facts kept), TRUNCATED, plus the two above.
* Coverage (§16, §17.1): `requirement_token_count` is the distinct ids mentioned in REQUIRED and
  PREFERRED clauses plus the unresolved tokens there: the noun phrases after "experience
  with/in", "knowledge of", "proficiency in", "familiarity with", ... and the tech-shaped words
  (inner capitals, ".js"/".NET", "C++"-like, unlisted acronyms) that resolved to nothing.
* Scalars: seniority `title_seniority(title, meta_level=meta.experience_level)`; internship from
  the metadata or the title; family `meta.job_family`, else the titles index; industry
  `classify_industry(company, company_description)` (a "{"-led description is junk and the
  posting's about section stands in); remote policy from `work_type`.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass, field
from typing import Literal, NamedTuple, Optional, Sequence

from pydantic import Field, field_validator

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.aliases import normalize_surface
from matcher.graph.model import SkillGraph
from matcher.ingest.language import detect_language
from matcher.ingest.normalize_text import normalize_text
from matcher.models import (
    ClearanceGate,
    DegreeLevel,
    EducationRequirement,
    Frozen,
    JobPosting,
    JobRequirement,
    LicenseGate,
    Location,
    LocationGate,
    Necessity,
    RemotePolicy,
    SkillKind,
    Track,
    WorkAuthGate,
)
from matcher.normalize.resolver import Mention, Resolver
from matcher.parse._text import Token, fold, tokens, words
from matcher.parse.config import ParseConfig
from matcher.parse.education import degree_matches
from matcher.parse.industry import classify_industry
from matcher.parse.posting_config import PostingTable
from matcher.parse.seniority import is_internship_text, title_seniority
from matcher.parse.taxonomy_extras import TitleIndex

__all__ = ["PostingMeta", "clean_description", "parse_posting"]

_FIELD = "field."
_ORDER = {Necessity.REQUIRED: 0, Necessity.PREFERRED: 1, Necessity.IMPLIED: 2}
_MAX_REQUIREMENTS = 1000
_MAX_TOKENS = 1000
_MAX_GATES = 20


# ─── metadata ─────────────────────────────────────────────────────────────────────────────


_WORK_TYPES = {
    "remote": "remote", "fullyremote": "remote", "remotefirst": "remote", "teletravail": "remote",
    "hybrid": "hybrid", "hybride": "hybrid",
    "onsite": "onsite", "inoffice": "onsite", "inperson": "onsite", "office": "onsite", "surplace": "onsite",
    "presentiel": "onsite",
}


class PostingMeta(Frozen):
    """The posting's metadata columns (Tailrd `scraped_jobs`). `work_type` accepts the stored
    spellings ("Remote", "Hybrid", "On Site", "on-site") and reads anything else as unknown."""

    company: str = Field(default="", max_length=2000)
    company_description: str = Field(default="", max_length=20000)
    locations: tuple[Location, ...] = Field(default=(), max_length=50)
    work_type: Literal["remote", "hybrid", "onsite", ""] = ""
    employment_type: str = Field(default="", max_length=200)
    experience_level: str = Field(default="", max_length=200)
    job_family: Optional[str] = Field(default=None, max_length=200)

    @field_validator("work_type", mode="before")
    @classmethod
    def _work_type(cls, v: object) -> str:
        if not isinstance(v, str):
            return ""
        key = re.sub(r"[\s_\-]+", "", fold(v))
        return _WORK_TYPES.get(key, "")

    @field_validator("company", "company_description", "employment_type", "experience_level", mode="before")
    @classmethod
    def _text(cls, v: object) -> str:
        return v if isinstance(v, str) else ""


# ─── text cleaning ────────────────────────────────────────────────────────────────────────

_ENTITY = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6});")
_TAG = re.compile(r"<\s*/?\s*[A-Za-z!][^<>]{0,2000}>")
_DROP_BODY = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_LIST_ITEM = re.compile(r"<\s*li\b[^>]*>", re.IGNORECASE)
_BLOCK = re.compile(
    r"<\s*/?\s*(?:br|p|div|ul|ol|li|h[1-6]|tr|table|tbody|thead|section|article|header|footer|blockquote|"
    r"pre|hr|dl|dt|dd)\b[^>]*>",
    re.IGNORECASE,
)
_INLINE_BULLET = re.compile(r"[ \t]+[•▪●◦■►➤✓✔][ \t]+")


class Cleaned(NamedTuple):
    text: str
    truncated: bool


def _cut_at_cap(raw: str, table: PostingTable) -> bool:
    stripped = raw.rstrip()
    if not any(stripped.endswith(marker) for marker in table.truncation.markers if marker):
        return False
    lengths = {len(raw), len(stripped)}
    return any(abs(length - cap) <= table.truncation.tolerance for cap in table.truncation.caps for length in lengths)


def _decode_html(text: str) -> str:
    for _ in range(2):  # "&amp;lt;p&amp;gt;" is escaped twice
        if not _ENTITY.search(text):
            break
        text = html.unescape(text)
    if _TAG.search(text):
        text = _COMMENT.sub(" ", text)
        text = _DROP_BODY.sub("\n", text)
        text = _LIST_ITEM.sub("\n- ", text)
        text = _BLOCK.sub("\n", text)
        text = _TAG.sub(" ", text)
        if _ENTITY.search(text):
            text = html.unescape(text)
    return text


def clean_description(description: object, table: PostingTable) -> Cleaned:
    """Plain, normalised text of a stored description and whether it was cut short."""
    if not isinstance(description, str) or not description.strip():
        return Cleaned("", False)
    raw = description
    truncated = _cut_at_cap(raw, table)
    if len(raw) > table.max_chars:
        raw = raw[: table.max_chars]
        truncated = True
    text = _decode_html(raw)
    text = _INLINE_BULLET.sub("\n- ", text)
    text = normalize_text(text)
    if truncated:
        text = text.rstrip()
        changed = True
        while changed:
            changed = False
            for marker in table.truncation.markers:
                if marker and text.endswith(marker):
                    text = text[: -len(marker)].rstrip()
                    changed = True
        if text and text[-1].isalnum():  # the cap fell inside a word: "JavaScript" cut to "Java"
            text = re.sub(r"\S+$", "", text).rstrip()
    return Cleaned(text, truncated)


# ─── sections ─────────────────────────────────────────────────────────────────────────────

_BULLET = re.compile(r"^\s*(?:[-*+>]|\d{1,2}[.)])\s+")
_MARKUP = re.compile(r"^[#*_=\s]+|[#*_=\s]+$")
_LABEL = re.compile(r"^([^:\n]{1,80}):\s*(.*)$")
_DEFAULTS = {
    "required": Necessity.REQUIRED,
    "preferred": Necessity.PREFERRED,
    "responsibilities": Necessity.IMPLIED,
    "other": Necessity.IMPLIED,
}
_READ = frozenset(_DEFAULTS)  # sections whose mentions are requirements


def _header_kind(text: str, table: PostingTable, *, contains: bool) -> Optional[str]:
    """The section kind a header text names, or None. `contains` (a line ending in ":") also
    finds a phrase inside the line ("Requirements for this role include:")."""
    sections = table.sections
    ws = tuple(words(text))
    if not ws or len(ws) > sections.max_header_words:
        return None
    kind = sections.index.get(ws)
    if kind is not None:
        return kind
    stripped = tuple(w for w in ws if w not in sections.filler_words)
    hit = sections.stripped_index.get(stripped) if stripped else None
    if hit is not None:
        return hit[1]
    if contains:
        order = {"preferred": 0, "required": 1, "responsibilities": 2, "about": 3, "ignored": 4}
        best: Optional[tuple[int, int, str]] = None
        for phrase, phrase_kind in sections.index.items():
            n = len(phrase)
            if any(ws[i:i + n] == phrase for i in range(len(ws) - n + 1)):
                key = (n, -order[phrase_kind])
                if best is None or key > best[:2]:
                    best = (n, -order[phrase_kind], phrase_kind)
        if best is not None:
            return best[2]
    return None


class _Line(NamedTuple):
    text: str  # content, bullet marker removed
    kind: str  # required | preferred | responsibilities | other | about | ignored
    bullet: bool


def _sectioned(text: str, table: PostingTable) -> list[_Line]:
    out: list[_Line] = []
    current = "other"
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        bullet = bool(_BULLET.match(line))
        body = _BULLET.sub("", line, count=1).strip() if bullet else line
        if not body:
            continue
        plain = _MARKUP.sub("", body).strip()
        if not bullet and plain and plain[-1] not in ".,;!" and not _LABEL.match(plain):
            kind = _header_kind(plain, table, contains=False)
            if kind is not None:
                current = kind
                continue
        label = _LABEL.match(plain)
        if label is not None:
            head = _MARKUP.sub("", label.group(1)).strip()
            rest = label.group(2).strip()
            kind = _header_kind(head, table, contains=not rest)
            if kind is not None:
                if not bullet:
                    current = kind
                if rest:
                    out.append(_Line(rest, kind, bullet))
                continue
        out.append(_Line(body, current, bullet))
    return out


# ─── statements and clauses ───────────────────────────────────────────────────────────────

_ABBREVIATIONS = frozenset({
    "e", "g", "eg", "i", "ie", "etc", "vs", "incl", "approx", "u", "s", "us", "sr", "jr", "inc", "ltd", "co",
    "no", "st", "dr", "mr", "ms", "mrs", "ex", "min", "max", "yrs", "yr", "esp", "dept", "fig", "p", "ph", "b",
    "m", "a", "c", "d", "eng", "sc", "approx", "corp", "mt", "ft", "pt", "hr", "hrs", "wk", "mo",
})
_SENTENCE_END = re.compile(r"[.!?]+(?=\s+[\"'(\[]?[A-Z0-9À-Ý])|[;]")


_MAX_STATEMENT = 600  # characters: bounds the per-statement work on absurd unpunctuated text


def _bounded(piece: str) -> list[str]:
    """`piece` in chunks of at most `_MAX_STATEMENT` characters, cut after a comma or a space."""
    out: list[str] = []
    while len(piece) > _MAX_STATEMENT:
        cut = max(piece.rfind(", ", 0, _MAX_STATEMENT), piece.rfind(" ", 0, _MAX_STATEMENT))
        cut = cut + 1 if cut > 0 else _MAX_STATEMENT
        out.append(piece[:cut].strip())
        piece = piece[cut:].strip()
    if piece:
        out.append(piece)
    return [chunk for chunk in out if chunk]


def _statements(line: str) -> list[str]:
    """`line` cut at ";" and at sentence ends (not after an abbreviation or an initial)."""
    out: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(line):
        if match.group() == ".":
            before = re.search(r"([A-Za-z]+)$", line[max(start, match.start() - 12):match.start()])
            if before is not None and fold(before.group(1)) in _ABBREVIATIONS:
                continue
        piece = line[start:match.end()].strip()
        if piece:
            out.extend(_bounded(piece))
        start = match.end()
    tail = line[start:].strip()
    if tail:
        out.extend(_bounded(tail))
    return out


_CLAUSE_BREAK = re.compile(r"(?<!\d),|,(?!\d)|[()\[\]:]|\s[-–]\s|\b(?:but|while|whereas|however|mais|tandis)\b",
                           re.IGNORECASE)
_LIST_WORDS = frozenset({
    "and", "or", "as", "well", "also", "etc", "nor", "either", "both", "plus", "et", "ou", "the", "a", "an", "any",
    "e", "g", "eg", "ie", "i", "such", "like", "including", "incl", "with", "in", "of",
})
_CONNECTIVES = frozenset({"and", "or", "et", "ou", "as", "nor"})


@dataclass
class _Clause:
    start: int
    end: int
    toks: list[Token]  # unmasked word tokens
    first_masked: bool  # the clause opens with a mention
    mentions: int
    cue: Optional[str] = None  # implied | negated | preferred | required
    necessity: Optional[Necessity] = None


def _clause_spans(stmt: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _CLAUSE_BREAK.finditer(stmt):
        if match.start() > start:
            spans.append((start, match.start()))
        start = match.end()
    if start < len(stmt):
        spans.append((start, len(stmt)))
    return [(a, b) for a, b in spans if stmt[a:b].strip()]


_NO_WORDS = frozenset({"no", "aucun", "aucune"})
_NEEDED_WORDS = frozenset({"required", "needed", "necessary", "requis", "requise", "necessaire"})


def _find_cue(ws: Sequence[str], all_ws: Sequence[str], table: PostingTable) -> Optional[str]:
    """The clause's modal kind. Implied and negated phrases are read over every word (a phrase that
    is itself a skill, "willingness to learn", still makes the clause IMPLIED); preferred and required
    phrases only outside skill mentions ("asset management" is no "asset")."""
    modal = table.modals.tuples
    for i, word in enumerate(all_ws):  # "No Kubernetes experience required", "no prior AWS knowledge needed"
        if word in _NO_WORDS and any(w in _NEEDED_WORDS for w in all_ws[i + 1:]):
            return "negated"
    for kind in ("implied", "negated", "preferred", "required"):
        ws_kind = all_ws if kind in ("implied", "negated") else ws
        for phrase in modal[kind]:
            n = len(phrase)
            if any(tuple(ws_kind[i:i + n]) == phrase for i in range(len(ws_kind) - n + 1)):
                return kind
    return None


def _cue_necessity(cue: str, section: str) -> Necessity:
    if cue in ("implied", "negated"):
        return Necessity.IMPLIED
    if cue == "preferred" or section == "preferred":
        return Necessity.PREFERRED
    return Necessity.REQUIRED


def _masked(tok: Token, spans: Sequence[tuple[int, int]]) -> bool:
    return any(a < tok.end and tok.start < b for a, b in spans)


def _clauses(stmt: str, section: str, spans: Sequence[tuple[int, int]], skill_spans: Sequence[tuple[int, int]],
             table: PostingTable) -> list[_Clause]:
    """The statement's clauses with a necessity each (module docstring)."""
    all_toks = tokens(stmt)
    clauses: list[_Clause] = []
    for a, b in _clause_spans(stmt):
        inside = [t for t in all_toks if a <= t.start and t.end <= b]
        free = [t for t in inside if not _masked(t, spans)]
        first_masked = bool(inside) and _masked(inside[0], spans)
        count = sum(1 for s, e in skill_spans if a <= s < b)
        clause = _Clause(a, b, free, first_masked, count)
        clause.cue = _find_cue([t.word for t in free], [t.word for t in inside], table)
        clauses.append(clause)
    default = _DEFAULTS.get(section, Necessity.IMPLIED)

    def pure(c: _Clause) -> bool:
        return c.mentions > 0 and all(t.word in _LIST_WORDS for t in c.toks)

    for i, clause in enumerate(clauses):
        if clause.cue is not None:
            clause.necessity = _cue_necessity(clause.cue, section)
            continue
        forward: Optional[Necessity] = None
        for j in range(i + 1, len(clauses)):
            later = clauses[j]
            if later.cue is not None:
                opens_list = later.first_masked or (bool(later.toks) and later.toks[0].word in _CONNECTIVES and
                                                    later.toks[0].start - later.start <= 2)
                if opens_list and all(pure(c) for c in clauses[i + 1:j]):
                    forward = _cue_necessity(later.cue, section)
                break
        previous = clauses[i - 1] if i > 0 else None
        if forward is not None:
            clause.necessity = forward
        elif pure(clause) and previous is not None and (previous.cue is not None or pure(previous)):
            clause.necessity = previous.necessity or default
        else:
            clause.necessity = default
    return clauses


def _necessity_at(clauses: Sequence[_Clause], offset: int, default: Necessity) -> Necessity:
    for clause in clauses:
        if clause.start <= offset < clause.end:
            return clause.necessity or default
    return default


def _clause_at(clauses: Sequence[_Clause], offset: int) -> Optional[_Clause]:
    for clause in clauses:
        if clause.start <= offset < clause.end:
            return clause
    return None


# ─── OR-groups ────────────────────────────────────────────────────────────────────────────

# A leading "." is the period closing an initialism ("P.Eng. or EIT"): the statement split has
# already cut at every real sentence end.
_GAP_OR = re.compile(r"^\.?\s*(?:,\s*)?(?:and\s*/\s*or|or\s*/\s*and|or|ou|/)\s*$", re.IGNORECASE)
_GAP_COMMA = re.compile(r"^\.?\s*,\s*$")
_GAP_AND = re.compile(r"^\.?\s*(?:,\s*)?(?:and|et|&|as\s+well\s+as|plus)\s*$", re.IGNORECASE)


def _gap(text: str) -> Optional[str]:
    if _GAP_OR.match(text):
        return "or"
    if _GAP_COMMA.match(text):
        return "comma"
    if _GAP_AND.match(text):
        return "and"
    return None


def _equivalent_after(text: str, table: PostingTable) -> bool:
    """`text` (what follows a mention) opens with "or (an/any/other) equivalent/similar/..."."""
    ws = words(text[:80])
    if not ws:
        return False
    i = 0
    if ws[0] in ("and",) and len(ws) > 1 and ws[1] == "or":
        i = 1
    if ws[i] not in ("or", "ou"):
        return False
    rest = [w for w in ws[i + 1:i + 5] if w not in ("a", "an", "any", "the", "some", "un", "une", "l", "tout")]
    return bool(rest) and rest[0] in table.or_groups.equivalent_set


def _lead_in(text: str, table: PostingTable) -> bool:
    """Whether `text` (before a run) ends with a lead-in such as "such as" (up to 3 words after it,
    "one of the following:")."""
    ws = words(text[-120:])
    for phrase in table.or_groups.lead_in_tuples:
        n = len(phrase)
        for back in range(0, 4):
            end = len(ws) - back
            if end - n >= 0 and tuple(ws[end - n:end]) == phrase:
                return True
    return False


def _runs(stmt: str, mentions: Sequence[Mention]) -> list[list[tuple[int, Optional[str]]]]:
    """Maximal runs of mentions joined by list separators: [(index, gap before it)]."""
    runs: list[list[tuple[int, Optional[str]]]] = []
    for i, mention in enumerate(mentions):
        if i > 0:
            gap = _gap(stmt[mentions[i - 1].end:mention.start])
            if gap is not None and runs:
                runs[-1].append((i, gap))
                continue
        runs.append([(i, None)])
    return runs


def _groups(stmt: str, mentions: Sequence[Mention], table: PostingTable) -> list[tuple[list[int], bool]]:
    """OR-groups of the statement: (mention indexes, or_equivalent)."""
    out: list[tuple[list[int], bool]] = []
    for run in _runs(stmt, mentions):
        members = [i for i, _ in run]
        gaps = [g for _, g in run[1:]]
        last = mentions[members[-1]]
        equivalent = _equivalent_after(stmt[last.end:], table)
        if len(members) > 1 and _lead_in(stmt[: mentions[members[0]].start], table):
            out.append((members, equivalent))
            continue
        if len(members) > 1 and gaps[-1] == "or" and all(g == "comma" for g in gaps[:-1]):
            out.append((members, equivalent))
            continue
        if equivalent and all(g == "comma" for g in gaps):
            out.append((members, True))
            continue
        # "X or Y" / "X/Y" pairs inside a longer list
        chain: list[int] = [members[0]]
        for (i, g) in run[1:]:
            if g == "or":
                chain.append(i)
                continue
            if len(chain) > 1:
                out.append((chain, False))
            chain = [i]
        if len(chain) > 1:
            out.append((chain, equivalent and chain[-1] == members[-1]))
        elif equivalent and chain[-1] == members[-1]:
            out.append((chain, True))
    return out


# ─── years ────────────────────────────────────────────────────────────────────────────────

_NUM = r"(\d{1,2}|[A-Za-zÀ-ſ]+)"
_YEARS = re.compile(
    r"(?:(?P<pre>minimum(?:\s+of)?|min\.?|at\s+least|over|more\s+than|au\s+moins|plus\s+de|minimum\s+de)\s+)?"
    rf"(?:\(\s*)?{_NUM}(?:\s*\))?\s*(?:\+|plus\b)?\s*(?:(?:-|–|to|à|a)\s*(?:\(\s*)?{_NUM}(?:\s*\))?\s*\+?\s*)?"
    r"(?:\(\s*\d{1,2}\s*\)\s*)?"
    r"(?P<unit>years?|yrs?|ans?|ann[ée]es?)(?:['’]s?)?(?![A-Za-z])",
    re.IGNORECASE,
)
_NOT_BEFORE = frozenset({"within", "past", "last", "next", "first", "every", "per", "after", "ago", "than", "dans",
                         "depuis", "for"})


class _Years(NamedTuple):
    value: int
    start: int
    end: int


def _number(text: Optional[str], table: PostingTable) -> Optional[int]:
    if text is None:
        return None
    if text.isdigit():
        return int(text)
    return table.years.number_words.get(fold(text))


def _years_in(stmt: str, table: PostingTable) -> list[_Years]:
    out: list[_Years] = []
    for match in _YEARS.finditer(stmt):
        low = _number(match.group(2), table)
        if low is None or low > 60:
            continue
        before = words(stmt[: match.start()])
        if match.group("pre") is None and before and before[-1] in _NOT_BEFORE:
            continue
        after = [w for w in words(stmt[match.end(): match.end() + 60]) if w not in ("of", "de", "d", "s")]
        if after and after[0] in table.years.not_experience_set:
            continue
        if re.match(r"\s*-\s*(?:year|an)\b", stmt[match.end() - len(match.group("unit")) - 1:match.end()]) and (
                after and after[0] in table.years.not_experience_set):
            continue
        out.append(_Years(low, match.start(), match.end()))
    return out


# ─── occurrences ──────────────────────────────────────────────────────────────────────────


@dataclass
class _Occurrence:
    skill_id: str
    necessity: Necessity
    order: int
    surface: str
    group: Optional[int] = None  # statement-local group key, made global by the caller
    equivalent: bool = False
    years: Optional[int] = None


@dataclass
class _Credential:
    cred_id: str
    necessity: Necessity
    holding: bool
    order: int
    group: Optional[int] = None
    equivalent: bool = False
    surface: str = ""


@dataclass
class _EduHit:
    level: DegreeLevel
    necessity: Necessity
    fields: tuple[str, ...]
    equivalent: bool


@dataclass
class _State:
    occurrences: list[_Occurrence] = field(default_factory=list)
    credentials: list[_Credential] = field(default_factory=list)
    totals: list[int] = field(default_factory=list)
    education: list[_EduHit] = field(default_factory=list)
    resolved_ids: set[str] = field(default_factory=set)
    unresolved: dict[str, str] = field(default_factory=dict)  # normalized -> surface
    next_group: int = 0
    order: int = 0


# ─── education ────────────────────────────────────────────────────────────────────────────

_POSSESSIVE_DEGREE = re.compile(r"(?:['’]s|s)$|\bof\b", re.IGNORECASE)


def _degree_levels(stmt: str, fields: Sequence[Mention], parse_config: ParseConfig) -> list[tuple[DegreeLevel, int]]:
    table = parse_config.posting.education
    toks = tokens(stmt)
    ws = [t.word for t in toks]
    has_cue = any(w in table.cue_set for w in ws) or bool(fields)
    out: list[tuple[DegreeLevel, int]] = []
    for match in degree_matches(stmt, parse_config):
        if match.level is DegreeLevel.NONE:
            continue
        text = stmt[match.start:match.end]
        cased_short = text.replace(".", "").isalpha() and not text.islower() and len(text.replace(".", "")) <= 5
        word_form = text.strip().lower().replace(".", "")
        bare = word_form in ("master", "bachelor", "doctor", "associate", "diploma", "masters", "bachelors")
        if bare and not has_cue:
            continue
        if not (cased_short or _POSSESSIVE_DEGREE.search(text) or has_cue):
            continue
        out.append((match.level, match.start))
    for phrase, level in table.phrase_tuples:
        n = len(phrase)
        for i in range(len(ws) - n + 1):
            if tuple(ws[i:i + n]) == phrase:
                out.append((level, toks[i].start))
                break
    if not out and fields:
        match = re.search(r"\b(?:degree|dipl[oô]me)\s+(?:in|en|of)\b", stmt, re.IGNORECASE)
        if match is not None:
            out.append((table.degree_in_field, match.start()))
    return out


# ─── unresolved tokens ────────────────────────────────────────────────────────────────────

_TECH_SHAPE = re.compile(
    r"(?<![\w.])(?:"
    r"[a-z]+[A-Z][A-Za-z0-9]*"  # iOS, jQuery, macOS
    r"|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*"  # GraphQL, TensorFlow
    r"|[A-Za-z][A-Za-z0-9]*\.(?:js|JS|io|NET|net)\b"  # Vue.js, ASP.NET
    r"|\.NET"
    r"|[A-Za-z]{1,6}(?:\+\+|#)"  # C++, F#
    r"|[A-Z]{2,6}(?:/[A-Z]{2,6})?"  # ETL, CI/CD
    r")(?![\w])"
)
_PROPER = re.compile(r"^(?:Mc|Mac|O)[A-Z]")


def _record_unresolved(state: _State, surface: str) -> None:
    surface = " ".join(surface.split())[:200]
    key = normalize_surface(surface)
    if key and key not in state.unresolved and len(state.unresolved) < _MAX_TOKENS:
        state.unresolved[key] = surface


def _unresolved_in_clause(stmt: str, clause: _Clause, spans: Sequence[tuple[int, int]], state: _State,
                          table: PostingTable) -> None:
    rules = table.unresolved
    free = clause.toks
    ws = [t.word for t in free]
    shouting = sum(1 for t in free if t.raw.isupper() and len(t.raw) > 2) > max(2, len(free) // 2)
    # (a) the noun phrase after a trigger
    for phrase in rules.trigger_tuples:
        n = len(phrase)
        for i in range(len(ws) - n + 1):
            if tuple(ws[i:i + n]) != phrase:
                continue
            tail_start = free[i + n - 1].end
            tail = stmt[tail_start:clause.end]
            for item_match in re.finditer(r"[^,/&;]+", tail):
                piece = item_match.group()
                parts = re.split(r"\b(?:and|or|et|ou)\b", piece)
                offset = tail_start + item_match.start()
                cursor = 0
                for part in parts:
                    start = offset + piece.index(part, cursor)
                    cursor = piece.index(part, cursor) + len(part)
                    item_toks = [t for t in tokens(part)]
                    if not item_toks:
                        continue
                    if any(_masked(Token(start + t.start, start + t.end, t.word, t.raw), spans) for t in item_toks):
                        continue
                    kept = list(item_toks)
                    while kept and kept[0].word in rules.lead_set:
                        kept.pop(0)
                    kept = kept[:4]
                    if not kept or all(t.word in rules.generic_set or t.word in rules.lead_set or t.word.isdigit()
                                       for t in kept):
                        continue
                    _record_unresolved(state, part[kept[0].start:kept[-1].end])
            break
    # (b) tech-shaped words
    if shouting:
        return
    text = stmt[clause.start:clause.end]
    for match in _TECH_SHAPE.finditer(text):
        a, b = clause.start + match.start(), clause.start + match.end()
        if any(s < b and a < e for s, e in spans):
            continue
        token = match.group()
        if _PROPER.match(token):
            continue
        if token.isupper() and all(part in rules.acronym_set for part in token.split("/")):
            continue
        if token.replace("/", "").isupper() and token.upper() in rules.acronym_set:
            continue
        _record_unresolved(state, token)


# ─── one statement ────────────────────────────────────────────────────────────────────────


def _holds_credential(clause: Optional[_Clause], mention: Mention, stmt: str, table: PostingTable) -> bool:
    """Whether the clause shows the credential is to be held: a holding word anywhere in it, the
    credential's own words included ("valid driver's licence", "CPA designation"), or the credential
    opening the clause ("RN with 3+ years")."""
    if clause is None:
        return False
    ws = words(stmt[clause.start:clause.end])
    for phrase in table.holding_tuples:
        n = len(phrase)
        if any(tuple(ws[i:i + n]) == phrase for i in range(len(ws) - n + 1)):
            return True
    head = stmt[clause.start:mention.start]
    return not words(head) or words(head) in (["a"], ["an"], ["be", "a"], ["be", "an"])


def _read_statement(stmt: str, section: str, state: _State, *, resolver: Resolver, graph: SkillGraph,
                    parse_config: ParseConfig) -> None:
    table = parse_config.posting
    skills = list(resolver.extract(stmt))
    # A statement that is nothing but a list ("- Rust", "Go, Rust or C++") is read as a skills list,
    # where an ambiguous name may open it; "- Excel at communication" is a sentence and stays prose.
    listed = resolver.extract(stmt, context="list")
    if len(listed) > len(skills) and all(
            tok.word in _LIST_WORDS for tok in tokens(stmt) if not _masked(tok, [(m.start, m.end) for m in listed])):
        skills = list(listed)
    fields =[m for m in resolver.extract(stmt, include_fields=True) if m.canonical_id.startswith(_FIELD)]
    levels = _degree_levels(stmt, fields, parse_config)
    schooling = levels or (fields and any(w in table.education.cue_set for w in words(stmt)))
    if schooling:  # an education statement: a skill that is also the named field is the field here
        skills = [m for m in skills if not any(f.start < m.end and m.start < f.end for f in fields)]
    spans = [(m.start, m.end) for m in (*skills, *fields)]
    skill_spans = [(m.start, m.end) for m in skills]
    clauses = _clauses(stmt, section, spans, skill_spans, table)
    default = _DEFAULTS[section]

    # OR-groups (statement-local, made global here)
    group_of: dict[int, tuple[int, bool]] = {}
    for members, equivalent in _groups(stmt, skills, table):
        key = state.next_group
        state.next_group += 1
        for i in members:
            group_of[i] = (key, equivalent)

    # years
    per_skill: dict[int, int] = {}
    for years in _years_in(stmt, table):
        after = [i for i, m in enumerate(skills) if m.start >= years.end]
        assigned = False
        if after:
            first = after[0]
            between = stmt[years.end:skills[first].start]
            if len(words(between)) <= 8 and not re.search(r"[;.]", between):
                run = [first]
                for i in range(first + 1, len(skills)):
                    if _gap(stmt[skills[i - 1].end:skills[i].start]) is None:
                        break
                    run.append(i)
                for i in run:
                    per_skill[i] = min(per_skill.get(i, years.value), years.value)
                assigned = True
        if not assigned:
            before = [i for i, m in enumerate(skills) if m.end <= years.start]
            if before:
                last = before[-1]
                between = stmt[skills[last].end:years.start]
                if len(words(between)) <= 1 and re.fullmatch(r"[\s(:\-–,]*\w*[\s(:\-–,]*", between):
                    per_skill[last] = min(per_skill.get(last, years.value), years.value)
                    assigned = True
        if not assigned:
            nearby = words(stmt[years.end: years.end + 80])[:6]
            if any(w in table.years.experience_set for w in nearby) or any(
                    w in table.years.experience_set for w in words(stmt[max(0, years.start - 40):years.start])[-3:]):
                if _necessity_at(clauses, years.start, default) is Necessity.REQUIRED:
                    state.totals.append(years.value)

    # skills and credentials
    for i, mention in enumerate(skills):
        necessity = _necessity_at(clauses, mention.start, default)
        node = graph.node(mention.canonical_id)
        state.order += 1
        group = group_of.get(i)
        if node is not None and node.kind is SkillKind.CREDENTIAL:
            holding = _holds_credential(_clause_at(clauses, mention.start), mention, stmt, table)
            state.credentials.append(_Credential(mention.canonical_id, necessity, holding, state.order,
                                                 None if group is None else group[0], bool(group and group[1]),
                                                 mention.surface))
        else:
            state.occurrences.append(_Occurrence(
                skill_id=mention.canonical_id, necessity=necessity, order=state.order, surface=mention.surface,
                group=None if group is None else group[0], equivalent=bool(group and group[1]),
                years=per_skill.get(i)))
        if necessity is not Necessity.IMPLIED:
            state.resolved_ids.add(mention.canonical_id)

    # education
    if levels:
        by_necessity: dict[Necessity, list[DegreeLevel]] = {}
        for level, offset in levels:
            necessity = _necessity_at(clauses, offset, default)
            by_necessity.setdefault(necessity, []).append(level)
        ws = words(stmt)
        equivalent = any(any(tuple(ws[i:i + len(p)]) == p for i in range(len(ws) - len(p) + 1))
                         for p in table.education.equivalent_tuples)
        field_ids = tuple(dict.fromkeys(m.canonical_id for m in fields))[:20]
        for necessity, found in by_necessity.items():
            if necessity is Necessity.IMPLIED:
                continue
            state.education.append(_EduHit(min(found, key=lambda lv: lv.rank), necessity, field_ids, equivalent))

    # unresolved requirement tokens
    for clause in clauses:
        if clause.necessity in (Necessity.REQUIRED, Necessity.PREFERRED):
            _unresolved_in_clause(stmt, clause, spans, state, table)


# ─── gates ────────────────────────────────────────────────────────────────────────────────


def _countries_named(text: str, table: PostingTable) -> list[str]:
    found: list[tuple[int, str]] = []
    for code, pattern in table.country_compiled:
        match = pattern.search(text)
        if match is not None:
            found.append((match.start(), code))
    return [code for _, code in sorted(found)]


def _country_of(match: re.Match[str]) -> Optional[str]:
    for name, value in match.groupdict().items():
        if value and name.startswith("c_"):
            return name[2:4]
    return None


_GATE_NEGATION = re.compile(r"\bnot\s+(?:required|necessary|needed|mandatory)\b|\bno\s+citizenship\b", re.IGNORECASE)


@dataclass
class _WorkAuth:
    countries: list[str] = field(default_factory=list)
    citizenship: bool = False
    refused: bool = False
    offered: bool = False
    stated: bool = False

    def add(self, codes: Sequence[str]) -> None:
        for code in codes:
            if code not in self.countries:
                self.countries.append(code)


def _work_auth_statement(stmt: str, auth: _WorkAuth, posting_countries: Sequence[str], table: PostingTable) -> None:
    rules = table.work_authorization
    ws = set(words(stmt))
    if any(p.search(stmt) for p in table.refused_compiled):
        auth.refused = True
        auth.stated = True
    elif any(p.search(stmt) for p in table.offered_compiled):
        auth.offered = True
        auth.stated = True
    negated = bool(_GATE_NEGATION.search(stmt))
    resident = any(p.search(stmt) for p in table.resident_compiled)
    if resident and not negated:
        auth.stated = True
        auth.add(_countries_named(stmt, table))
    citizenship = None
    for pattern in table.citizenship_compiled:
        citizenship = pattern.search(stmt)
        if citizenship is not None:
            break
    if citizenship is not None and not resident and not negated and ws & rules.citizenship_set:
        named = _country_of(citizenship)
        codes = [named] if named else (_countries_named(stmt, table) or list(posting_countries))
        auth.citizenship = True
        auth.stated = True
        auth.add(codes)
    if not negated and ws & rules.requirement_set:
        for pattern in table.authorized_compiled:
            match = pattern.search(stmt)
            if match is None:
                continue
            auth.stated = True
            named = _country_of(match)
            if named:
                auth.add([named])
            break


def _clearance_statement(stmt: str, posting_countries: Sequence[str], ladders: Sequence[Sequence[str]],
                         table: PostingTable) -> Optional[ClearanceGate]:
    rules = table.security_clearance
    compiled = rules.compiled
    if not any(p.search(stmt) for p in compiled["context"]):
        return None
    if any(p.search(stmt) for p in compiled["negation"]):
        return None
    named = [code for code, patterns in rules.country_compiled if any(p.search(stmt) for p in patterns)]
    taken: list[tuple[int, int]] = []
    tokens_found: list[str] = []
    unknown_country = False
    on_ladder = {token: (li, ti) for li, ladder in enumerate(ladders) for ti, token in enumerate(ladder)}
    for level in rules.levels:
        for pattern in level.compiled:
            for match in pattern.finditer(stmt):
                if any(a < match.end() and match.start() < b for a, b in taken):
                    continue
                taken.append((match.start(), match.end()))
                if len(level.countries) == 1:
                    country: Optional[str] = level.countries[0]
                else:
                    options = [c for c in named if c in level.countries]
                    local = [c for c in posting_countries if c in level.countries]
                    country = options[0] if len(options) == 1 else (local[0] if len(local) == 1 and not options
                                                                     else None)
                token = f"{country}_{level.suffix}" if country else None
                if token is None or token not in on_ladder:
                    unknown_country = True
                elif token not in tokens_found:
                    tokens_found.append(token)
    level_token = ""
    if tokens_found and not unknown_country:
        level_token = min(tokens_found, key=lambda t: (on_ladder[t][1], t))
    obtainable = any(p.search(stmt) for p in compiled["obtainable"])
    active = any(p.search(stmt) for p in compiled["active"]) and not obtainable
    return ClearanceGate(level=level_token, active_required=active)


def _preferred_statement(stmt: str, table: PostingTable) -> bool:
    ws = words(stmt)
    for phrase in table.modals.tuples["preferred"]:
        n = len(phrase)
        if any(tuple(ws[i:i + n]) == phrase for i in range(len(ws) - n + 1)):
            return True
    return False


# ─── assembly ─────────────────────────────────────────────────────────────────────────────


class _Unit(NamedTuple):
    key: str  # group label or skill id
    members: tuple[str, ...]
    idf: float


def _assemble(state: _State, graph: SkillGraph, config: EngineConfig, as_of: dt.date,
              flags: set[Flag]) -> list[JobRequirement]:
    occurrences = state.occurrences
    # merge groups that share a member (union-find over statement-local keys)
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first_group: dict[str, int] = {}
    for occ in occurrences:
        if occ.group is None:
            continue
        find(occ.group)
        seen = first_group.get(occ.skill_id)
        if seen is None:
            first_group[occ.skill_id] = occ.group
        else:
            a, b = find(seen), find(occ.group)
            if a != b:
                parent[max(a, b)] = min(a, b)
    for occ in occurrences:
        if occ.group is not None:
            occ.group = find(occ.group)

    standalone: dict[str, Necessity] = {}
    for occ in occurrences:
        if occ.group is None:
            best = standalone.get(occ.skill_id)
            if best is None or _ORDER[occ.necessity] < _ORDER[best]:
                standalone[occ.skill_id] = occ.necessity
    group_necessity: dict[int, Necessity] = {}
    for occ in occurrences:
        if occ.group is not None:
            best = group_necessity.get(occ.group)
            if best is None or _ORDER[occ.necessity] < _ORDER[best]:
                group_necessity[occ.group] = occ.necessity
    dissolved = {
        g for g, necessity in group_necessity.items()
        if any(occ.group == g and occ.skill_id in standalone and _ORDER[standalone[occ.skill_id]] <= _ORDER[necessity]
               for occ in occurrences)
    }
    for occ in occurrences:
        if occ.group in dissolved:
            occ.group = None
            occ.necessity = Necessity.IMPLIED
            occ.equivalent = False
        elif occ.group is not None:
            occ.necessity = group_necessity[occ.group]

    chosen: dict[str, _Occurrence] = {}
    years: dict[tuple[str, Necessity], int] = {}
    for occ in occurrences:
        if occ.years is not None:
            key = (occ.skill_id, occ.necessity)
            years[key] = min(years.get(key, occ.years), occ.years)
        current = chosen.get(occ.skill_id)
        if current is None:
            chosen[occ.skill_id] = occ
            continue
        better = (_ORDER[occ.necessity], occ.group is not None) < (_ORDER[current.necessity], current.group is not None)
        if better:
            chosen[occ.skill_id] = occ
    equivalent_groups = {occ.group for occ in occurrences if occ.group is not None and occ.equivalent}

    ordered = sorted(chosen.values(), key=lambda occ: (occ.order, occ.skill_id))
    labels: dict[int, str] = {}
    for occ in ordered:
        if occ.group is not None and occ.group not in labels:
            labels[occ.group] = f"or:{len(labels) + 1}"

    rows: list[dict] = []
    for occ in ordered[:_MAX_REQUIREMENTS]:
        min_years = years.get((occ.skill_id, occ.necessity))
        row_flags: tuple[Flag, ...] = ()
        node = graph.node(occ.skill_id)
        if min_years is not None and node is not None and node.release_year is not None:
            span = max(0, as_of.year - node.release_year)
            if min_years > span:
                min_years = span
                row_flags = (Flag.UNREALISTIC_REQUIREMENT,)
                flags.add(Flag.UNREALISTIC_REQUIREMENT)
        rows.append({
            "skill_id": occ.skill_id,
            "necessity": occ.necessity,
            "min_years": min(min_years, 60) if min_years is not None else None,
            "or_group": labels.get(occ.group) if occ.group is not None else None,
            "or_equivalent": occ.group in equivalent_groups if occ.group is not None else False,
            "surface_form": occ.surface[:2000],
            "weight_multiplier": 1.0,
            "flags": row_flags,
        })

    # overflow beyond the top N weighted units by IDF (SPEC §14.4)
    rules = config.scoring.posting
    unseen = config.scoring.idf.unseen_default
    units: dict[str, list[str]] = {}
    for row in rows:
        if row["necessity"] is Necessity.IMPLIED:
            continue
        units.setdefault(row["or_group"] or row["skill_id"], []).append(row["skill_id"])
    if len(units) > rules.max_weighted_requirements:
        ranked = sorted(
            (_Unit(key, tuple(sorted(members)), max(graph.idf_of(m, unseen) for m in members))
             for key, members in units.items()),
            key=lambda unit: (-unit.idf, unit.members[0], unit.key),
        )
        demoted = {unit.key for unit in ranked[rules.max_weighted_requirements:]}
        for row in rows:
            if row["necessity"] is not Necessity.IMPLIED and (row["or_group"] or row["skill_id"]) in demoted:
                row["necessity"] = Necessity.IMPLIED
                row["weight_multiplier"] = rules.overflow_weight_multiplier
        flags.add(Flag.REQUIREMENTS_TRUNCATED)
    return [JobRequirement(**row) for row in rows]


def _license_gates(state: _State, skill_groups: set[int]) -> tuple[tuple[str, ...], tuple[str, ...], list[LicenseGate]]:
    """Credentials -> (required ids, preferred ids, licence gates). Alternatives share a gate and
    one held member shows the whole group is to be held ("P.Eng. or EIT designation"). A credential
    with a non-credential alternative ("CPA or 5 years of accounting experience") or an "or
    equivalent" is required but gates nothing: the posting accepts another way in."""
    required: list[str] = []
    preferred: list[str] = []
    gate_groups: dict[object, list[str]] = {}
    surfaces: dict[str, str] = {}
    held_groups = {c.group for c in state.credentials if c.group is not None and c.holding}
    for cred in sorted(state.credentials, key=lambda c: c.order):
        holding = cred.holding or (cred.group is not None and cred.group in held_groups)
        if cred.necessity is Necessity.REQUIRED and holding:
            if cred.cred_id not in required:
                required.append(cred.cred_id)
            if cred.equivalent or (cred.group is not None and cred.group in skill_groups):
                continue
            key: object = ("g", cred.group) if cred.group is not None else ("c", cred.cred_id)
            members = gate_groups.setdefault(key, [])
            if cred.cred_id not in members:
                members.append(cred.cred_id)
                surfaces.setdefault(cred.cred_id, cred.surface)
        elif cred.necessity is Necessity.PREFERRED and cred.cred_id not in preferred:
            preferred.append(cred.cred_id)
    gates: list[LicenseGate] = []
    seen: set[tuple[str, ...]] = set()
    for members in gate_groups.values():
        ids = tuple(sorted(members))
        if ids not in seen:
            seen.add(ids)
            text = " or ".join(surfaces.get(i) or i for i in members)
            gates.append(LicenseGate(credential_ids=ids, surface_form=text[:2000]))
    required_ids = tuple(sorted(required))
    preferred_ids = tuple(sorted(c for c in preferred if c not in required))
    return required_ids, preferred_ids, gates


# ─── the parser ───────────────────────────────────────────────────────────────────────────

_REMOTE = {"remote": RemotePolicy.REMOTE, "hybrid": RemotePolicy.HYBRID, "onsite": RemotePolicy.ONSITE}


def _posting_countries(meta: PostingMeta) -> list[str]:
    out: list[str] = []
    for place in meta.locations:
        if place.country and place.country not in out:
            out.append(place.country)
    return out


def parse_posting(title: str, description: str, *, meta: PostingMeta, as_of: dt.date, resolver: Resolver,
                  graph: SkillGraph, config: EngineConfig, parse_config: ParseConfig, titles: TitleIndex) -> JobPosting:
    """One posting's `JobPosting` (see the module docstring)."""
    table = parse_config.posting
    title = title if isinstance(title, str) else ""
    meta = meta if isinstance(meta, PostingMeta) else PostingMeta()
    cleaned = clean_description(description, table)
    flags: set[Flag] = set()
    if cleaned.truncated:
        flags.add(Flag.TRUNCATED)
    lines = _sectioned(cleaned.text, table)
    posting_countries = _posting_countries(meta)

    state = _State()
    auth = _WorkAuth()
    clearances: list[ClearanceGate] = []
    ladders = config.gates.security_clearance.ladders
    boilerplate_chars = 0
    total_chars = 0
    about_text: list[str] = []
    for line in lines:
        total_chars += len(line.text)
        if line.kind in ("about", "ignored"):
            boilerplate_chars += len(line.text)
            if line.kind == "about":
                about_text.append(line.text)
        for stmt in _statements(line.text):
            if line.kind in _READ:
                _read_statement(stmt, line.kind, state, resolver=resolver, graph=graph, parse_config=parse_config)
            if line.kind == "preferred" or _preferred_statement(stmt, table):
                continue
            _work_auth_statement(stmt, auth, posting_countries, table)
            gate = _clearance_statement(stmt, posting_countries, ladders, table)
            if gate is not None and gate not in clearances:
                clearances.append(gate)

    skill_groups = {occ.group for occ in state.occurrences if occ.group is not None}
    certifications_required, certifications_preferred, license_gates = _license_gates(state, skill_groups)
    requirements = _assemble(state, graph, config, as_of, flags)

    gates: list = []
    if auth.stated:
        sponsorship = None
        if auth.refused and not auth.offered:
            sponsorship = False
        elif auth.offered and not auth.refused:
            sponsorship = True
        countries = list(auth.countries)
        if auth.citizenship and not countries:
            countries = list(posting_countries)
        gates.append(WorkAuthGate(countries=tuple(countries), sponsorship_available=sponsorship,
                                  citizenship_required=auth.citizenship))
    gates.extend(license_gates)
    gates.extend(clearances)
    if meta.work_type == "onsite" and meta.locations:
        gates.append(LocationGate(locations=meta.locations, onsite_required=True))
    gates = gates[:_MAX_GATES]

    # education requirement: REQUIRED wins over PREFERRED, the lowest level stated
    education = None
    for necessity in (Necessity.REQUIRED, Necessity.PREFERRED):
        hits = [hit for hit in state.education if hit.necessity is necessity]
        if hits:
            level = min((hit.level for hit in hits), key=lambda lv: lv.rank)
            field_ids = tuple(dict.fromkeys(f for hit in hits if hit.level is level for f in hit.fields))[:20]
            education = EducationRequirement(min_level=level, field_ids=field_ids,
                                             or_equivalent=any(hit.equivalent for hit in hits), necessity=necessity)
            break

    min_years_total = float(min(state.totals)) if state.totals else None

    seniority = title_seniority(title, parse_config, meta_level=meta.experience_level or None)
    level_text = fold(meta.experience_level or "")
    is_internship = (
        "intern" in level_text or "co op" in level_text.replace("-", " ") or "coop" in level_text
        or fold(meta.employment_type or "") in ("internship", "intern", "co-op", "coop")
        or is_internship_text(title, parse_config)
    )
    info = titles.lookup(title)
    family = (meta.job_family or "").strip() or (info.family if info is not None and info.family else None)
    company_description = meta.company_description.strip()
    if company_description.startswith("{"):
        company_description = ""
    context = company_description or " ".join(about_text)[:4000]
    industry_codes = classify_industry(meta.company, context, parse_config)[:20]

    distinct = {r.skill_id for r in requirements}
    if not requirements:
        flags.add(Flag.POSTING_UNSCORABLE)
    rules = table.boilerplate
    if (not cleaned.truncated and total_chars > 0 and len(distinct) < rules.max_requirements
            and boilerplate_chars / total_chars >= rules.min_share):
        flags.add(Flag.BOILERPLATE_ONLY)
    contradiction = table.contradiction
    stated_years = min_years_total
    if stated_years is None:
        required_years = [r.min_years for r in requirements if r.necessity is Necessity.REQUIRED and r.min_years is not None]
        stated_years = float(max(required_years)) if required_years else None
    if (seniority is not None and Track(seniority.track) is Track.IC and seniority.level <= contradiction.level.level
            and Track(contradiction.level.track) is Track.IC and stated_years is not None
            and stated_years >= contradiction.min_years):
        flags.add(Flag.CONTRADICTORY_REQUIREMENTS)

    language, _ = detect_language(cleaned.text) if cleaned.text else (None, 0.0)
    unresolved = tuple(state.unresolved[key] for key in sorted(state.unresolved))
    token_count = len(state.resolved_ids) + len(unresolved)

    return JobPosting(
        title_raw=title[:2000],
        title_canonical=titles.canonical_id(title),
        job_family=family[:200] if family else None,
        seniority=seniority,
        requirements=tuple(requirements),
        min_years_total=min(min_years_total, 60.0) if min_years_total is not None else None,
        education_requirement=education,
        certifications_required=certifications_required,
        certifications_preferred=certifications_preferred,
        industry_codes=tuple(industry_codes),
        locations=meta.locations,
        remote_policy=_REMOTE.get(meta.work_type, RemotePolicy.UNKNOWN),
        gates=tuple(gates),
        unresolved_requirement_tokens=unresolved,
        requirement_token_count=token_count,
        is_internship=is_internship,
        language=language if language in ("en", "fr") else "en",
        flags=tuple(flag for flag in Flag if flag in flags),
    )
