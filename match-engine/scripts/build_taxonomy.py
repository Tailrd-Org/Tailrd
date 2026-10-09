"""Build the engine's skill taxonomy from ESCO + O*NET + Tailrd's curated tables (Task 2.2).

SPEC §3: "Seed it from ESCO and/or O*NET ... Do not hand-write a taxonomy from scratch." This
script turns the public sources that scripts/fetch_sources.py puts under data/raw/ (git-ignored,
DECISIONS D-04) and the hand-curated config into the hash-pinned artifacts of data/taxonomy/:

    nodes.json.gz       every node (graph.loader format)
    idf.json            empty until Task 2.3 computes IDF from the posting corpus
    pmi_edges.json      the curated related edges (Task 2.3 adds the mined ones)
    resolver.json       ambiguous and cased keys with the casing each requires; blocked phrases
    titles.json         normalised job title -> O*NET-SOC code, SOC major group, job zone
    mos_crosswalk.json  military occupation code -> branch, title, O*NET-SOC codes, skill ids
    naics.json          NAICS 2022 code -> title
    MANIFEST.json       sha256 of each of the above, plus the input hashes
    SOURCES.md          versions, URLs, licences, input hashes and counts

Run from match-engine/ (after scripts/fetch_sources.py):

    .venv/Scripts/python.exe scripts/build_taxonomy.py [--raw DIR] [--out DIR] [--review DIR]

The output is a pure function of the raw inputs and the config: the same inputs give the same
bytes. Rules, in the order they apply:

1. Curated nodes (config/curated_taxonomy.yaml: skills, credentials, fields) come first. Their
   ids, labels, kinds, parents and release years are final, and their labels and aliases are
   exempt from every alias filter.
2. An ESCO concept or skill group, an O*NET commodity class or an O*NET example whose name
   normalises (`normalize_surface`, hyphen variants included) to a curated node's label or alias
   MERGES into that node: its names become the node's aliases (of an ESCO concept, its preferred
   labels only), its parents are inherited only when the curated node declares no `parents` key,
   and every reference to it points at the node.
3. Other ESCO concepts become `esco.<slug of the preferred label>` (a slug shared by several
   concepts gets `_` + the first 6 hex of each concept's UUID), skill groups `esco.group.<code>`
   (the four pillar roots are not nodes). Kinds: the transversal skills collection -> SOFT,
   programming languages -> TECHNOLOGY, other knowledge -> DOMAIN, skill/competence -> METHODOLOGY.
   Parents: ESCO broader relations.
4. Other O*NET examples become `skill.<slug>` (Technology Skills: TECHNOLOGY or TOOL by commodity
   class, see TECHNOLOGY_COMMODITIES; Tools Used: TOOL), parented by `cat.<commodity code>`. An
   example named like its own commodity class is that class's node.
5. Aliases: ESCO preferred, alternative and hidden labels (English and French), the preferred
   label without its trailing parenthetical, O*NET names without a vendor prefix or a trailing
   "software", and the two halves of a trailing acronym ("Structured query language SQL").
   Dropped: single-word ESCO alternative/hidden labels (unless they are the node's own preferred
   label), one-word French preferred labels unlike the English one, anything under
   `blocked_aliases`, and non-curated aliases of two characters or fewer.
   A non-curated node whose own label is blocked or that short is dropped; its children take its
   parents. When nodes claim the same key: a curated alias wins, then a node's own label, then
   the strongest claim (name > derived name > alternative label > hidden label > French
   alternative); a tie between different nodes is ambiguous and the key is dropped from all.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from matcher.graph.aliases import normalize_keep_case, normalize_surface, surface_variants  # noqa: E402
from matcher.graph.loader import write_artifacts  # noqa: E402
from matcher.graph.model import SkillNode  # noqa: E402
from matcher.graph.pmi import build_edges  # noqa: E402
from matcher.models import SkillKind  # noqa: E402

DEFAULT_RAW = ROOT / "data" / "raw"
DEFAULT_OUT = ROOT / "data" / "taxonomy"
DEFAULT_CURATED = ROOT / "config" / "curated_taxonomy.yaml"
DEFAULT_EDGES = ROOT / "config" / "curated_edges.yaml"
DEFAULT_GATES = ROOT / "config" / "gates.yaml"

EDGE_WEIGHT_RANGE = (0.4, 0.8)  # hand-curated related edges (D-23 credits clamp to this range)
MAX_SLUG = 100
KNOWLEDGE_MIN_IMPORTANCE = 3.0  # O*NET importance scale 1-5; 3 = "important"
KNOWLEDGE_TOP = 5  # knowledge areas per occupation that feed a military code's skill ids
COMMON_WORD_MIN_COUNT = 2  # lower-case occurrences in the source prose that make a word "common"
MOS_COMMON_KNOWLEDGE_SHARE = 0.25  # a knowledge area in more military codes than this says nothing
PROGRAMMING_ID = "skill.programming"  # curated "Computer programming": ESCO languages sit under it
GITATTRIBUTES = (
    "# These files are hash-pinned by MANIFEST.json: git must never rewrite their line endings\n"
    "# (core.autocrlf would turn LF into CRLF on a Windows checkout and break every sha256).\n"
    "* -text\n"
)


class BuildError(ValueError):
    """The raw inputs or the curated config cannot produce a valid taxonomy."""


# ─── tables ────────────────────────────────────────────────────────────────────────────────

# O*NET Technology Skills commodity classes (UNSPSC) whose examples are TECHNOLOGY: programming
# and development, databases, operating systems, networking, servers, security and cloud
# infrastructure. Every other class (office suites, spreadsheets, presentation, project
# management, CAD/CAM, accounting, ERP, CRM, graphics, video, analytics and other applications)
# is TOOL.
TECHNOLOGY_COMMODITIES = frozenset({
    "43232203",  # File versioning software
    "43232304",  # Data base management system software
    "43232305",  # Data base reporting software
    "43232306",  # Data base user interface and query software
    "43232311",  # Object oriented data base management software
    "43232312",  # Portal server software
    "43232313",  # Transaction server software
    "43232401",  # Configuration management software
    "43232402",  # Development environment software
    "43232403",  # Enterprise application integration software
    "43232404",  # Graphical user interface development software
    "43232405",  # Object or component oriented development software
    "43232406",  # Program testing software
    "43232407",  # Requirements analysis and system architecture software
    "43232408",  # Web platform development software
    "43232409",  # Compiler and decompiler software
    "43232608",  # Industrial control software
    "43232701",  # Application server software
    "43232704",  # Internet directory services software
    "43232801",  # Network monitoring software
    "43232802",  # Network operating system enhancement software
    "43232803",  # Optical network management software
    "43232804",  # Administration software
    "43232805",  # Internet protocol IP multimedia subsystem software
    "43232806",  # Cloud-based management software
    "43232901",  # Access software
    "43232902",  # Communications server software
    "43232905",  # LAN software
    "43232907",  # Storage networking software
    "43232908",  # Switch or router software
    "43232909",  # WAN switching software and firmware
    "43232910",  # Wireless software
    "43232911",  # Network connectivity terminal emulation software
    "43232912",  # Gateway software
    "43232913",  # Bridge software
    "43232915",  # Platform interconnectivity software
    "43232917",  # Software defined networking/ virtualization software
    "43233001",  # Filesystem software
    "43233002",  # Network operation system software
    "43233004",  # Operating system software
    "43233201",  # Authentication server software
    "43233203",  # Network security or virtual private network VPN management software
    "43233204",  # Network security and virtual private network VPN equipment software
    "43233205",  # Transaction security and virus protection software
    "43233206",  # Cloud-based protection or security software
    "43233402",  # Data conversion software
    "43233405",  # Device drivers or system software
    "43233407",  # Graphics card driver software
    "43233410",  # Printer driver software
    "43233414",  # Storage media loading software
    "43233415",  # Backup or archival software
    "43233701",  # Enterprise system management software
})

# SOC 2018 major groups (U.S. Bureau of Labor Statistics), keyed by the first two digits.
SOC_MAJOR_GROUPS = {
    "11": "Management Occupations",
    "13": "Business and Financial Operations Occupations",
    "15": "Computer and Mathematical Occupations",
    "17": "Architecture and Engineering Occupations",
    "19": "Life, Physical, and Social Science Occupations",
    "21": "Community and Social Service Occupations",
    "23": "Legal Occupations",
    "25": "Educational Instruction and Library Occupations",
    "27": "Arts, Design, Entertainment, Sports, and Media Occupations",
    "29": "Healthcare Practitioners and Technical Occupations",
    "31": "Healthcare Support Occupations",
    "33": "Protective Service Occupations",
    "35": "Food Preparation and Serving Related Occupations",
    "37": "Building and Grounds Cleaning and Maintenance Occupations",
    "39": "Personal Care and Service Occupations",
    "41": "Sales and Related Occupations",
    "43": "Office and Administrative Support Occupations",
    "45": "Farming, Fishing, and Forestry Occupations",
    "47": "Construction and Extraction Occupations",
    "49": "Installation, Maintenance, and Repair Occupations",
    "51": "Production Occupations",
    "53": "Transportation and Material Moving Occupations",
    "55": "Military Specific Occupations",
}

# DMDC service codes (crosswalk "SVC") for primary occupation codes: Army MOS/AOC, Marine Corps
# MOS, Navy ratings/NEC/NOBC and officer designators, Air Force and Space Force specialty codes,
# Coast Guard ratings. Additional identifiers (ASI, SQI, SEI, prefixes), DoD conversion codes and
# civilian series (OPM, NSPS) are left out.
MILITARY_BRANCHES = {"A": "army", "M": "marine_corps", "N": "navy", "P": "navy", "F": "air_force",
                     "H": "space_force", "C": "coast_guard"}
BRANCH_ORDER = ("army", "marine_corps", "navy", "air_force", "space_force", "coast_guard")
_AFSC = re.compile(r"(\d[A-Z]\d)(\d)(\d)[A-Z]?")  # enlisted AFSC: career field, skill level, shredout
_AFSC_LEVEL_WORDS = re.compile(r"[\s,]+(Helper|Apprentice|Journeyman|Craftsman|Superintendent)\b.*$")

# Claim strengths: lower wins a contested key (rule 5 of the module docstring).
CURATED, NAME, DERIVED, ALT, HIDDEN, FR_NAME, FR_ALT = 0, 1, 2, 3, 4, 3, 5
_FILTERED_SINGLE = frozenset({ALT, HIDDEN, FR_ALT})  # single-word claims of these strengths drop

_TRAILING_PAREN = re.compile(r"^(.*?\S)\s*\(([^()]*)\)\s*$")
_ACRONYM = re.compile(r"[A-Z][A-Z0-9]{1,9}")


# ─── small helpers ─────────────────────────────────────────────────────────────────────────


def slugify(text: str) -> str:
    """ASCII id fragment: `C++` -> `cpp`, `C#` -> `csharp`, `.NET` -> `dotnet`, `Node.js` -> `node_js`."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold().strip()
    text = re.sub(r"^\.(?=\w)", "dot", text)
    text = text.replace("++", "pp").replace("+", "plus").replace("#", "sharp").replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:MAX_SLUG].rstrip("_") or "x"


def strip_parenthetical(label: str) -> Optional[str]:
    """`Python (computer programming)` -> `Python`; None when there is no trailing parenthetical."""
    match = _TRAILING_PAREN.match(label)
    return match.group(1).strip() if match else None


def _brand_shaped(text: str) -> bool:
    """A product name rather than a description: `SAP`, `HubSpot`, `Adobe Creative Cloud`, `@Risk`,
    but not `Billing` or `Inventory management`."""
    tokens = text.split()
    if len(tokens) == 1:
        token = tokens[0]
        return (any(ch.isupper() for ch in token[1:]) or any(ch.isdigit() for ch in token)
                or any(ch in "@+#&" for ch in token))
    for index, token in enumerate(tokens):
        if any(ch.isdigit() for ch in token) or any(ch.isupper() for ch in token[1:]):
            return True
        if index and token[:1].isupper():
            return True
    return False


def strip_software(name: str) -> Optional[str]:
    """`SAP software` -> `SAP`; kept only for product-like names, so `Payroll software` stays."""
    if not name.lower().endswith(" software"):
        return None
    rest = name[: -len(" software")].strip()
    return rest if rest and _brand_shaped(rest) else None


_FUNCTION_WORDS = frozenset({"of", "and", "for", "the", "to", "in", "on", "a", "an", "&", "with", "by"})


def split_acronym(name: str) -> Optional[tuple[str, str]]:
    """`Structured query language SQL` -> (`SQL`, `Structured query language`).

    The last token must be an upper-case acronym spelled by ALL the words before it (at least
    two): every word gives its initial (the first word may give its second letter, eXtensible),
    a word may give further letters after its initial (HyperText Markup Language -> HTML), and a
    function word (of, and, for, ...) may give nothing. A product name that merely ends in a
    category acronym ("HarrisData Human Resources Information System HRIS") does not split.
    """
    tokens = name.split()
    if len(tokens) < 3 or not _ACRONYM.fullmatch(tokens[-1]):
        return None
    words = [word.lower() for word in tokens[:-1]]
    letters = [ch for ch in tokens[-1].lower() if ch.isalpha()]
    if len(letters) < 2:
        return None

    def take(i: int, j: int) -> bool:  # letters[i:] spelled by words[j:]
        if j == len(words):
            return i == len(letters)
        word = words[j]
        if word in _FUNCTION_WORDS and take(i, j + 1):
            return True
        if i == len(letters):
            return False
        for start in (0, 1) if j == 0 else (0,):
            if start < len(word) and word[start] == letters[i] and more(i + 1, start + 1, j):
                return True
        return False

    def more(i: int, position: int, j: int) -> bool:  # optionally more letters from words[j]
        if take(i, j + 1):
            return True
        if i < len(letters):
            found = words[j].find(letters[i], position)
            while found >= 0:
                if more(i + 1, found + 1, j):
                    return True
                found = words[j].find(letters[i], found + 1)
        return False

    return (tokens[-1], " ".join(tokens[:-1])) if take(0, 0) else None


def strip_vendor(name: str, vendors: Sequence[str]) -> Optional[str]:
    """`Microsoft Excel` -> `Excel` for the first (longest) vendor prefix that leaves a name."""
    for vendor in vendors:
        if name.startswith(vendor + " "):
            rest = name[len(vendor) + 1:].strip()
            if rest and rest.lower() != "software":
                return rest
    return None


def onet_forms(name: str, vendors: Sequence[str]) -> list[tuple[str, int, bool]]:
    """(surface, strength, may_merge) of every name an O*NET example or class is known by.

    The name itself, its vendor-free form and an acronym's expansion may merge the concept into a
    curated node; a bare acronym ("PMP", "EPIC": too many meanings) and a name stripped of
    "software" ("Statistics software" -> "Statistics": a category word) are aliases only.
    """
    forms = [(name, NAME, True)]
    base = strip_software(name)
    if base:
        forms.append((base, DERIVED, False))
    for candidate, strength in ((name, NAME), (base, DERIVED)):
        split = split_acronym(candidate) if candidate else None
        if split:
            forms.extend(((split[0], strength, False), (split[1], strength, True)))
    vendor_free = strip_vendor(base or name, vendors)
    if vendor_free:
        forms.append((vendor_free, DERIVED, True))
        trimmed = strip_software(vendor_free)
        if trimmed:
            forms.append((trimmed, DERIVED, False))
        split = split_acronym(trimmed or vendor_free)
        if split:
            forms.extend(((split[0], DERIVED, False), (split[1], DERIVED, True)))
    return forms


def _keys(surface: str) -> tuple[str, ...]:
    return surface_variants(normalize_surface(surface))


def _text_sha256(path: Path) -> str:
    """sha256 of a text file with line endings normalised (stable across git checkouts)."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def _read_rows(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    quoting = csv.QUOTE_MINIMAL if delimiter == "," else csv.QUOTE_NONE
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=delimiter, quoting=quoting))


def _split_labels(value: str) -> list[str]:
    return [part.strip() for part in value.replace("\r", "\n").split("\n") if part.strip()]


# ─── curated config ────────────────────────────────────────────────────────────────────────

_NODE_KEYS = {"id", "label", "kind", "aliases", "parents", "release_year"}
_SECTIONS = {"skills": ("skill.", None), "credentials": ("cred.", SkillKind.CREDENTIAL),
             "fields": ("field.", SkillKind.DOMAIN)}


@dataclass(frozen=True)
class CuratedNode:
    id: str
    label: str
    kind: SkillKind
    aliases: tuple[str, ...]
    parents: Optional[tuple[str, ...]]  # None: inherit the merged sources' parents
    release_year: Optional[int]


@dataclass
class Curated:
    nodes: list[CuratedNode]
    ambiguous: dict[str, str]
    blocked: frozenset[str]  # normalised surfaces
    vendors: tuple[str, ...]  # longest first
    onet_knowledge: dict[str, str]
    no_merge: frozenset[str]
    blocked_phrases: tuple[str, ...] = ()  # as written, "[A-Z]" expanded (resolver.json)
    generic_titles: frozenset[str] = frozenset()  # title keys that name no occupation


_LETTER_SLOT = "[A-Z]"  # in a blocked phrase: one phrase per capital letter ("Building [A-Z]")


def _expand_phrases(phrases: Iterable[str]) -> tuple[str, ...]:
    out = []
    for phrase in phrases:
        phrase = " ".join(phrase.split())
        if _LETTER_SLOT in phrase:
            out.extend(phrase.replace(_LETTER_SLOT, chr(code)) for code in range(ord("A"), ord("Z") + 1))
        else:
            out.append(phrase)
    return tuple(sorted(set(out), key=lambda p: (normalize_surface(p), p)))


def _strings(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise BuildError(f"{where} must be a list of non-empty strings (quote YAML words like no/on)")
    return value


def load_curated(path: Path) -> Curated:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != 1:
        raise BuildError(f"{path}: expected a mapping with format: 1")
    nodes: list[CuratedNode] = []
    seen: set[str] = set()
    for section, (prefix, required_kind) in _SECTIONS.items():
        for record in doc.get(section) or []:
            where = f"{path.name}: {section}: {record.get('id') if isinstance(record, dict) else record!r}"
            if not isinstance(record, dict) or set(record) - _NODE_KEYS or not {"id", "label", "kind"} <= set(record):
                raise BuildError(f"{where}: a node needs id, label, kind (optional aliases, parents, release_year)")
            node_id, label = record["id"], record["label"]
            if not isinstance(node_id, str) or not node_id.startswith(prefix) or node_id in seen:
                raise BuildError(f"{where}: ids in {section} start with {prefix!r} and are unique")
            if not isinstance(label, str) or not label.strip():
                raise BuildError(f"{where}: label must be a non-empty string")
            try:
                kind = SkillKind(record["kind"])
            except ValueError as exc:
                raise BuildError(f"{where}: unknown kind {record['kind']!r}") from exc
            if required_kind is not None and kind is not required_kind:
                raise BuildError(f"{where}: {section} are {required_kind.value}")
            year = record.get("release_year")
            if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
                raise BuildError(f"{where}: release_year must be an integer")
            parents = record.get("parents")
            seen.add(node_id)
            nodes.append(CuratedNode(
                id=node_id, label=label, kind=kind,
                aliases=tuple(_strings(record.get("aliases"), f"{where}: aliases")),
                parents=None if parents is None else tuple(_strings(parents, f"{where}: parents")),
                release_year=year,
            ))
    ambiguous = doc.get("ambiguous") or {}
    if not isinstance(ambiguous, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in ambiguous.items()):
        raise BuildError(f"{path.name}: ambiguous must map keys to their casing (quote yes/no/on/off)")
    for key, casing in ambiguous.items():
        if normalize_surface(casing) != normalize_surface(key):
            raise BuildError(f"{path.name}: ambiguous key {key!r} and casing {casing!r} differ")
    knowledge = doc.get("onet_knowledge") or {}
    if not isinstance(knowledge, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in knowledge.items()):
        raise BuildError(f"{path.name}: onet_knowledge must map O*NET knowledge areas to node ids")
    vendors = _strings(doc.get("onet_vendor_prefixes"), f"{path.name}: onet_vendor_prefixes")
    phrases = _expand_phrases(_strings(doc.get("blocked_phrases"), f"{path.name}: blocked_phrases"))
    curated_keys = {key for node in nodes for surface in (node.label, *node.aliases) for key in _keys(surface)}
    for phrase in phrases:
        clash = set(_keys(phrase)) & curated_keys
        if clash:
            raise BuildError(f"{path.name}: blocked phrase {phrase!r} is also a curated label or alias")
    generic = _strings(doc.get("generic_titles"), f"{path.name}: generic_titles")
    return Curated(
        nodes=nodes,
        ambiguous={normalize_surface(key): normalize_keep_case(casing) for key, casing in ambiguous.items()},
        blocked=frozenset(normalize_surface(s) for s in _strings(doc.get("blocked_aliases"), "blocked_aliases")),
        vendors=tuple(sorted(set(vendors), key=lambda v: (-len(v), v))),
        onet_knowledge=dict(knowledge),
        no_merge=frozenset(_strings(doc.get("no_merge"), f"{path.name}: no_merge")),
        blocked_phrases=phrases,
        generic_titles=frozenset(key for title in generic for key in _keys(title)),
    )


def load_curated_edges(path: Path) -> list[tuple[str, str, float]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != 1 or not isinstance(doc.get("edges", []), list):
        raise BuildError(f"{path}: expected format: 1 and an edges list")
    edges = []
    low, high = EDGE_WEIGHT_RANGE
    for item in doc.get("edges") or []:
        if (not isinstance(item, list) or len(item) != 3 or not all(isinstance(x, str) for x in item[:2])
                or isinstance(item[2], bool) or not isinstance(item[2], (int, float))):
            raise BuildError(f"{path.name}: edge {item!r} must be [id, id, weight]")
        a, b, weight = item[0], item[1], float(item[2])
        if a == b:
            raise BuildError(f"{path.name}: edge {a} -> {b} is a self-loop")
        if not low <= weight <= high:
            raise BuildError(f"{path.name}: edge {a} -> {b} weight {weight} outside [{low}, {high}]")
        edges.append((a, b, weight))
    return edges


# ─── raw inputs ────────────────────────────────────────────────────────────────────────────


@dataclass
class EscoConcept:
    uri: str
    pref: str
    alt: list[str]
    hidden: list[str]
    kind: SkillKind
    is_group: bool
    code: str = ""
    pref_fr: str = ""
    alt_fr: list[str] = field(default_factory=list)
    hidden_fr: list[str] = field(default_factory=list)


@dataclass
class Raw:
    record: dict[str, Any]
    esco: dict[str, EscoConcept]  # uri -> concept (skills and groups)
    roots: frozenset[str]  # pillar root uris
    broader: dict[str, list[str]]  # uri -> broader uris
    french: bool
    tech: list[dict[str, str]]
    tools: list[dict[str, str]]
    occupations: list[dict[str, str]]
    alternate_titles: list[dict[str, str]]
    reported_titles: list[dict[str, str]]
    job_zones: dict[str, int]
    knowledge: list[dict[str, str]]
    military: list[dict[str, str]]
    naics: list[tuple[str, str]]
    common_words: frozenset[str]  # lowercase words of ordinary prose in the sources (see _common_words)


def _download(record: Mapping[str, Any], name: str) -> Optional[Mapping[str, Any]]:
    entry = record.get("downloads", {}).get(name)
    if not entry or entry.get("error"):
        return None
    return entry


def read_raw(raw_dir: Path) -> Raw:
    record_path = raw_dir / "fetch_record.json"
    if not record_path.exists():
        raise BuildError(f"{record_path} is missing: run scripts/fetch_sources.py first")
    record = json.loads(record_path.read_text(encoding="utf-8"))

    esco_en = _download(record, "esco_en")
    onet = _download(record, "onet_db")
    naics = _download(record, "naics_2022")
    for name, entry in (("esco_en", esco_en), ("onet_db", onet), ("naics_2022", naics)):
        if entry is None:
            raise BuildError(f"required source {name} was not fetched (see fetch_record.json)")
    en_dir = raw_dir / esco_en["extracted_to"]
    esco_fr = _download(record, "esco_fr")
    fr_dir = raw_dir / esco_fr["extracted_to"] if esco_fr else None

    transversal = {row["conceptUri"] for row in _read_rows(en_dir / "transversalSkillsCollection_en.csv")}
    esco: dict[str, EscoConcept] = {}
    prose: list[str] = []  # descriptions and task statements: the common-word corpus
    for row in _read_rows(en_dir / "skills_en.csv"):
        prose.append(row.get("description", ""))
        if row["conceptUri"] in transversal:
            kind = SkillKind.SOFT
        elif row["skillType"] == "knowledge":
            kind = SkillKind.DOMAIN
        else:
            kind = SkillKind.METHODOLOGY
        esco[row["conceptUri"]] = EscoConcept(
            uri=row["conceptUri"], pref=row["preferredLabel"].strip(), alt=_split_labels(row["altLabels"]),
            hidden=_split_labels(row["hiddenLabels"]), kind=kind, is_group=False)
    roots = set()
    for row in _read_rows(en_dir / "skillGroups_en.csv"):
        code = row["code"].strip()
        if len(code) == 1:  # the pillar roots: S, K, T, L
            roots.add(row["conceptUri"])
            continue
        initial = code[:1]
        kind = (SkillKind.SOFT if initial == "T" else SkillKind.METHODOLOGY if initial == "S"
                else SkillKind.DOMAIN)
        esco[row["conceptUri"]] = EscoConcept(
            uri=row["conceptUri"], pref=row["preferredLabel"].strip(), alt=_split_labels(row["altLabels"]),
            hidden=_split_labels(row["hiddenLabels"]), kind=kind, is_group=True, code=code)
    french = False
    if fr_dir is not None:
        for name in ("skills_fr.csv", "skillGroups_fr.csv"):
            path = fr_dir / name
            if not path.exists():
                continue
            for row in _read_rows(path):
                concept = esco.get(row["conceptUri"])
                if concept is None:
                    continue
                concept.pref_fr = row["preferredLabel"].strip()
                concept.alt_fr = _split_labels(row["altLabels"])
                concept.hidden_fr = _split_labels(row["hiddenLabels"])
                french = True
    broader: dict[str, list[str]] = defaultdict(list)
    for row in _read_rows(en_dir / "broaderRelationsSkillPillar_en.csv"):
        broader[row["conceptUri"]].append(row["broaderUri"])

    onet_dir = raw_dir / onet["extracted_to"]
    candidates = sorted(p for p in onet_dir.iterdir() if p.is_dir() and re.fullmatch(r"db_\d+_\d+_text", p.name))
    if not candidates:
        raise BuildError(f"no O*NET db_*_text directory under {onet_dir}")
    db = candidates[-1]

    military_rows: list[dict[str, str]] = []
    military = _download(record, "onet_military")
    if military:
        files = sorted((raw_dir / military["extracted_to"]).rglob("*.csv"))
        if files:
            military_rows = _read_rows(files[0])

    naics_rows = []
    for row in _read_rows(raw_dir / naics["converted_to"], "\t"):
        naics_rows.append((row["code"].strip(), " ".join(row["title"].split())))

    job_zones = {}
    for row in _read_rows(db / "Job Zones.txt", "\t"):
        if row["Job Zone"].strip().isdigit():
            job_zones[row["O*NET-SOC Code"]] = int(row["Job Zone"])

    occupations = _read_rows(db / "Occupation Data.txt", "\t")
    prose.extend(row.get("Description", "") for row in occupations)
    if (en_dir / "occupations_en.csv").exists():
        prose.extend(row.get("description", "") for row in _read_rows(en_dir / "occupations_en.csv"))
    if (db / "Task Statements.txt").exists():
        prose.extend(row.get("Task", "") for row in _read_rows(db / "Task Statements.txt", "\t"))

    return Raw(
        record=record, esco=esco, roots=frozenset(roots), broader=dict(broader), french=french,
        tech=_read_rows(db / "Technology Skills.txt", "\t"), tools=_read_rows(db / "Tools Used.txt", "\t"),
        occupations=occupations,
        alternate_titles=_read_rows(db / "Alternate Titles.txt", "\t"),
        reported_titles=_read_rows(db / "Sample of Reported Titles.txt", "\t"),
        job_zones=job_zones, knowledge=_read_rows(db / "Knowledge.txt", "\t"),
        military=military_rows, naics=naics_rows, common_words=_common_words(prose),
    )


_PROSE_WORD = re.compile(r"(?<![A-Za-z])[a-z][a-z]+(?![A-Za-z])")


def _common_words(texts: Iterable[str]) -> frozenset[str]:
    """Words written in lower case at least COMMON_WORD_MIN_COUNT times across the ESCO and O*NET
    descriptions and task statements: ordinary vocabulary ("billing", "edge", "levels"), as
    opposed to product names, which those texts capitalise or never use."""
    counts: dict[str, int] = defaultdict(int)
    for text in texts:
        for word in _PROSE_WORD.findall(text):
            counts[word] += 1
    return frozenset(word for word, count in counts.items() if count >= COMMON_WORD_MIN_COUNT)


# ─── node assembly ─────────────────────────────────────────────────────────────────────────


@dataclass
class Claim:
    surface: str
    strength: int
    group: int  # claims from one source concept share a group (the single-word rule's "own label")
    derived: bool = False  # a shortened name (bare label, vendor-free, acronym half): a common word drops


@dataclass
class Draft:
    id: str
    label: str
    kind: SkillKind
    source: str
    curated: bool
    explicit_parents: Optional[tuple[str, ...]] = None
    release_year: Optional[int] = None
    parent_refs: set[tuple[str, str]] = field(default_factory=set)  # ("uri", uri) | ("cat", code) | ("id", id)
    claims: list[Claim] = field(default_factory=list)
    pref_keys: dict[int, set[str]] = field(default_factory=dict)  # claim group -> own preferred-label keys


@dataclass
class Report:
    counts: dict[str, Any] = field(default_factory=dict)
    merges: list[tuple[str, str, str]] = field(default_factory=list)  # (source, name, curated id)
    dropped_nodes: list[tuple[str, str, str]] = field(default_factory=list)  # (id, label, reason)
    ambiguous_keys: list[tuple[str, list[str]]] = field(default_factory=list)
    label_ties: list[tuple[str, list[str]]] = field(default_factory=list)
    derived_single_words: list[tuple[str, str]] = field(default_factory=list)  # (surface, node id)
    auto_ambiguous: dict[str, str] = field(default_factory=dict)  # common-word labels -> their casing
    seconds: float = 0.0


class Assembler:
    def __init__(self, curated: Curated, report: Report, common_words: frozenset[str] = frozenset()) -> None:
        self.curated = curated
        self.common_words = common_words
        self.report = report
        self.drafts: dict[str, Draft] = {}
        self.curated_keys: dict[str, str] = {}
        self.uri_map: dict[str, str] = {}
        self.cat_map: dict[str, str] = {}
        self.example_map: dict[str, str] = {}  # O*NET example text -> node id
        self.final_ids: set[str] = set()  # set once finish() has run
        self._onet_keys: dict[str, str] = {}  # normalised O*NET example name -> node id
        self._group = 0

    def _next_group(self) -> int:
        self._group += 1
        return self._group

    def _claim(self, draft: Draft, surface: str, strength: int, group: int, own: bool = False,
               derived: bool = False) -> None:
        surface = " ".join(surface.split())
        if not surface:
            return
        draft.claims.append(Claim(surface, strength, group, derived))
        if own:
            draft.pref_keys.setdefault(group, set()).add(normalize_surface(surface))

    def _curated_target(self, name: str, derived: Iterable[str] = ()) -> Optional[str]:
        """The curated node a source concept merges into: by its full name, else by a derived
        name (bare label, vendor-free name, acronym) that would itself survive as an alias, so a
        bare "ML" never merges the ML language into machine learning."""
        for position, surface in enumerate((name, *derived)):
            for key in _keys(surface):
                if position and (key in self.curated.blocked or len(key) <= 2):
                    continue
                target = self.curated_keys.get(key)
                if target is not None:
                    return target
        return None

    # curated ------------------------------------------------------------------------------

    def add_curated(self) -> None:
        for node in self.curated.nodes:
            draft = Draft(id=node.id, label=node.label, kind=node.kind, source="curated", curated=True,
                          explicit_parents=node.parents, release_year=node.release_year)
            group = self._next_group()
            for surface in (node.label, *node.aliases):
                self._claim(draft, surface, CURATED, group, own=True)
                for key in _keys(surface):
                    owner = self.curated_keys.setdefault(key, node.id)
                    if owner != node.id:
                        raise BuildError(f"curated nodes {owner} and {node.id} both claim {key!r}")
            self.drafts[node.id] = draft

    # ESCO ---------------------------------------------------------------------------------

    def add_esco(self, raw: Raw) -> None:
        own: dict[str, Draft] = {}
        pending: list[tuple[EscoConcept, str]] = []  # concepts that get their own node: (concept, slug)
        for uri in sorted(raw.esco):
            concept = raw.esco[uri]
            bare = strip_parenthetical(concept.pref)
            target = None
            if uri not in self.curated.no_merge:
                target = self._curated_target(concept.pref, [bare] if bare else [])
            if target is not None:
                self.uri_map[uri] = target
                self._esco_claims(self.drafts[target], concept, merged=True)
                self.drafts[target].parent_refs.update(("uri", b) for b in raw.broader.get(uri, ()))
                self.report.merges.append(("esco", concept.pref, target))
            elif concept.is_group:
                node_id = "esco.group." + concept.code.lower()
                own[uri] = Draft(id=node_id, label=concept.pref, kind=concept.kind, source="esco", curated=False)
                self.uri_map[uri] = node_id
            else:
                pending.append((concept, slugify(concept.pref)))
        by_slug: dict[str, list[EscoConcept]] = defaultdict(list)
        for concept, slug in pending:
            by_slug[slug].append(concept)
        for slug, concepts in sorted(by_slug.items()):
            ids = {c.uri: f"esco.{slug}" for c in concepts}
            if len(concepts) > 1:
                ids = _suffixed(slug, concepts)
            for concept in concepts:
                own[concept.uri] = Draft(id=ids[concept.uri], label=concept.pref, kind=concept.kind, source="esco",
                                         curated=False)
                self.uri_map[concept.uri] = ids[concept.uri]
        for uri, draft in own.items():
            if draft.id in self.drafts:
                raise BuildError(f"ESCO id {draft.id} collides with another node")
            self._esco_claims(draft, raw.esco[uri])
            draft.parent_refs.update(("uri", b) for b in raw.broader.get(uri, ()))
            self.drafts[draft.id] = draft
        for root in raw.roots:
            self.uri_map[root] = ""

    def _esco_claims(self, draft: Draft, concept: EscoConcept, *, merged: bool = False) -> None:
        """The concept's labels as claims on `draft` (DECISIONS D-26). A one-word French preferred
        label stays only when it is the English one ("Python"): otherwise it is often an ordinary
        French word ("morale", "saisie"). A concept merged into a curated node brings its preferred
        labels only: its alternative and hidden labels too often name a neighbour of the curated
        sense ("web design" under computer programming, "make business decisions" under marketing)."""
        group = self._next_group()
        english = {normalize_surface(concept.pref)}
        for pref, strength in ((concept.pref, NAME), (concept.pref_fr, FR_NAME)):
            if not pref:
                continue
            bare = strip_parenthetical(pref)
            for surface, derived in ((pref, False), (bare, True)):
                if not surface:
                    continue
                key = normalize_surface(surface)
                if strength == NAME:
                    english.add(key)
                elif " " not in key and key not in english:
                    continue
                self._claim(draft, surface, strength, group, own=True, derived=derived)
        if merged:
            return
        for labels, strength in ((concept.alt, ALT), (concept.hidden, HIDDEN), (concept.alt_fr, FR_ALT),
                                 (concept.hidden_fr, FR_ALT)):
            for label in labels:
                self._claim(draft, label, strength, group)

    # O*NET --------------------------------------------------------------------------------

    def add_onet(self, raw: Raw) -> None:
        commodities: dict[str, str] = {}
        tech_codes: set[str] = set()
        examples: dict[str, set[str]] = defaultdict(set)  # example -> commodity codes
        tech_examples: dict[str, set[str]] = defaultdict(set)  # Technology Skills example -> its codes
        for row in raw.tech:
            example, code = row["Example"].strip(), row["Commodity Code"]
            commodities.setdefault(code, row["Commodity Title"].strip())
            tech_codes.add(code)
            examples[example].add(code)
            tech_examples[example].add(code)
        for row in raw.tools:
            commodities.setdefault(row["Commodity Code"], row["Commodity Title"].strip())
            examples[row["Example"].strip()].add(row["Commodity Code"])

        for code in sorted(commodities):
            title = commodities[code]
            forms = onet_forms(title, ())
            target = None if f"onet:{title}" in self.curated.no_merge else self._curated_target(
                title, [surface for surface, _, may_merge in forms[1:] if may_merge])
            group = self._next_group()
            if target is not None:
                self.cat_map[code] = target
                draft = self.drafts[target]
                self.report.merges.append(("onet-class", title, target))
            else:
                technology = code in TECHNOLOGY_COMMODITIES and code in tech_codes
                draft = Draft(id=f"cat.{code}", label=title, source="onet", curated=False,
                              kind=SkillKind.TECHNOLOGY if technology else SkillKind.TOOL)
                self.drafts[draft.id] = draft
                self.cat_map[code] = draft.id
            for index, (surface, strength, _) in enumerate(forms):
                self._claim(draft, surface, strength, group, own=strength == NAME, derived=index > 0)
            self._onet_keys.setdefault(normalize_surface(title), self.cat_map[code])

        # Technology Skills examples first, so a name in both files keeps its technology kind.
        for example in sorted(examples, key=lambda e: (e not in tech_examples, e)):
            codes = examples[example]
            forms = onet_forms(example, self.curated.vendors)
            key = normalize_surface(example)
            target = None
            if f"onet:{example}" not in self.curated.no_merge:
                target = self._curated_target(example, [surface for surface, _, may_merge in forms[1:] if may_merge])
            if target is not None:
                self.report.merges.append(("onet", example, target))
            elif key in self._onet_keys:  # named like its commodity class, or like another example
                target = self._onet_keys[key]
            if target is None:
                technology = bool(tech_examples.get(example, set()) & TECHNOLOGY_COMMODITIES)
                kind = SkillKind.TECHNOLOGY if technology else SkillKind.TOOL
                node_id = "skill." + slugify(example)
                if node_id in self.drafts:
                    node_id += "_" + hashlib.sha256(example.encode("utf-8")).hexdigest()[:6]
                if node_id in self.drafts:
                    raise BuildError(f"O*NET id {node_id} collides with another node")
                self.drafts[node_id] = Draft(id=node_id, label=example, kind=kind, source="onet", curated=False)
                target = node_id
            draft = self.drafts[target]
            group = self._next_group()
            for index, (surface, strength, _) in enumerate(forms):
                self._claim(draft, surface, strength, group, own=strength == NAME, derived=index > 0)
            draft.parent_refs.update(("cat", code) for code in codes)
            self.example_map[example] = target
            self._onet_keys.setdefault(key, target)

    # finishing ----------------------------------------------------------------------------

    def finish(self) -> list[SkillNode]:
        """Resolve parents, drop unusable nodes, settle contested aliases, return the nodes."""
        blocked = self.curated.blocked
        dropped: dict[str, str] = {}
        for draft in self.drafts.values():
            if draft.curated:
                continue
            key = normalize_surface(draft.label)
            reason = "blocked label" if key in blocked else "label of 2 characters or fewer" if len(key) <= 2 else ""
            if reason:
                dropped[draft.id] = reason
                self.report.dropped_nodes.append((draft.id, draft.label, reason))

        raw_parents = {draft.id: self._raw_parents(draft) for draft in self.drafts.values()}
        parents: dict[str, tuple[str, ...]] = {}
        for draft in self.drafts.values():
            if draft.id in dropped:
                continue
            if draft.explicit_parents is not None:
                resolved = set(draft.explicit_parents)
            else:
                resolved = _splice(raw_parents[draft.id], raw_parents, dropped, draft.id)
            resolved.discard(draft.id)
            for parent in resolved:
                if parent not in self.drafts or parent in dropped:
                    raise BuildError(f"{draft.id} has unknown parent {parent}")
            parents[draft.id] = tuple(sorted(resolved))

        aliases = self._settle_aliases(dropped)
        languages = self._programming_languages(parents)
        nodes = []
        for node_id in sorted(parents):
            draft = self.drafts[node_id]
            nodes.append(SkillNode(
                id=node_id, label=draft.label, parents=parents[node_id],
                kind=SkillKind.TECHNOLOGY if node_id in languages else draft.kind,
                aliases=aliases.get(node_id, ()), release_year=draft.release_year, source=draft.source,
            ))
        return nodes

    def _programming_languages(self, parents: Mapping[str, tuple[str, ...]]) -> set[str]:
        """ESCO concepts that are programming languages, which ESCO files as knowledge: labelled
        "... (computer programming)", or under computer programming (PROGRAMMING_ID) or under such a
        language ("Common Lisp" under "Lisp"). They are TECHNOLOGY, like the curated languages."""
        found: set[str] = set()
        candidates = sorted(node_id for node_id, draft in self.drafts.items()
                            if node_id in parents and draft.source == "esco" and not node_id.startswith("esco.group."))
        changed = True
        while changed:
            changed = False
            for node_id in candidates:
                if node_id in found:
                    continue
                if self.drafts[node_id].label.endswith("(computer programming)") or any(
                        parent == PROGRAMMING_ID or parent in found for parent in parents[node_id]):
                    found.add(node_id)
                    changed = True
        return found

    def _raw_parents(self, draft: Draft) -> set[str]:
        out = set()
        for kind, ref in draft.parent_refs:
            if kind == "uri":
                mapped = self.uri_map.get(ref)
                if mapped is None:
                    raise BuildError(f"{draft.id}: broader concept {ref} is not in the ESCO files")
            elif kind == "cat":
                mapped = self.cat_map[ref]
            else:
                mapped = ref
            if mapped:
                out.add(mapped)
        return out

    def _settle_aliases(self, dropped: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
        blocked = self.curated.blocked
        label_owner: dict[str, list[str]] = defaultdict(list)
        for draft in self.drafts.values():
            if draft.id not in dropped:
                label_owner[normalize_surface(draft.label)].append(draft.id)
        for key, owners in sorted(label_owner.items()):
            if len(owners) > 1:
                self.report.label_ties.append((key, sorted(owners)))

        # key -> [(strength, node id, surface)]
        claims: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        for draft in self.drafts.values():
            if draft.id in dropped:
                continue
            own_keys = set().union(*draft.pref_keys.values()) if draft.pref_keys else set()
            for claim in draft.claims:
                key = normalize_surface(claim.surface)
                if not key:
                    continue
                if claim.strength != CURATED:
                    if key in blocked or len(key) <= 2:
                        continue
                    if claim.derived and key in self.common_words:  # "Billing", "Edge", "Paint"
                        continue
                    if (claim.strength in _FILTERED_SINGLE and " " not in key
                            and key not in draft.pref_keys.get(claim.group, ()) and key not in own_keys):
                        continue
                claims[key].append((claim.strength, draft.id, claim.surface))

        winners: dict[str, list[tuple[int, str]]] = defaultdict(list)  # node id -> [(strength, surface)]
        for key in sorted(claims):
            entries = claims[key]
            curated = {node_id for strength, node_id, _ in entries if strength == CURATED}
            if curated:
                owner = min(curated)
            elif key in label_owner:
                owners = label_owner[key]
                if len(owners) > 1:
                    continue  # the labels themselves are indexed; the resolver settles the tie
                owner = owners[0]
            else:
                best = min(strength for strength, _, _ in entries)
                owners = sorted({node_id for strength, node_id, _ in entries if strength == best})
                if len(owners) > 1:
                    self.report.ambiguous_keys.append((key, owners))
                    continue
                owner = owners[0]
            surfaces = sorted((strength, surface) for strength, node_id, surface in entries if node_id == owner)
            if surfaces:
                winners[owner].append(surfaces[0])
                if surfaces[0][0] == DERIVED and " " not in key and not self.drafts[owner].curated:
                    self.report.derived_single_words.append((surfaces[0][1], owner))

        out = {}
        for node_id, chosen in winners.items():
            label_key = normalize_surface(self.drafts[node_id].label)
            surfaces = sorted({surface for _, surface in chosen if normalize_surface(surface) != label_key},
                              key=lambda s: (normalize_surface(s), s))
            out[node_id] = tuple(surfaces)
        return out


def _suffixed(slug: str, concepts: Sequence[EscoConcept]) -> dict[str, str]:
    """`esco.<slug>_<first hex of the UUID>`: 6 hex digits, more if 6 still collide."""
    hexes = {c.uri: c.uri.rstrip("/").rsplit("/", 1)[-1].replace("-", "").lower() for c in concepts}
    width = 6
    while True:
        ids = {uri: f"esco.{slug}_{h[:width]}" for uri, h in hexes.items()}
        if len(set(ids.values())) == len(ids):
            return ids
        width += 2


def _splice(start: set[str], raw_parents: Mapping[str, set[str]], dropped: Mapping[str, str],
            node_id: str) -> set[str]:
    """Parents with each dropped node replaced by its own parents (transitively, cycle-safe)."""
    out: set[str] = set()
    stack, seen = list(start), {node_id}
    while stack:
        parent = stack.pop()
        if parent in seen:
            continue
        seen.add(parent)
        if parent in dropped:
            stack.extend(raw_parents.get(parent, ()))
        else:
            out.add(parent)
    return out


_PLAIN_WORD = re.compile(r"[A-Za-z]+")


def auto_ambiguous(nodes: Sequence[SkillNode], curated: Mapping[str, str]) -> dict[str, str]:
    """One-word names with a capital, on nodes that are not curated, are proper nouns: products
    ("Hyperion", "Safari"), tool and class labels ("Levels", "Skiffs"), languages ("Polish"),
    capital acronyms ("MAX"). Each becomes an ambiguous key with its written casing (SPEC §3.3):
    lower-case prose ("all levels", "analyze the data", "max 5") never resolves to it while a
    skills list still does, and since the resolver never fuzzes towards an ambiguous key, a near
    miss of an ordinary word ("skills" -> "Skiffs", "salary" -> "Safari") stays unresolved. A key
    a curated entry or another node also claims is left alone; the label's casing wins."""
    owners: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        for surface in (node.label, *node.aliases):
            owners[normalize_surface(surface)].add(node.id)
    out: dict[str, str] = {}
    for node in nodes:
        if node.source == "curated":
            continue
        for surface in (node.label, *node.aliases):
            if _PLAIN_WORD.fullmatch(surface) and not surface.islower():
                key = surface.lower()
                if key not in curated and owners[key] == {node.id}:
                    out.setdefault(key, surface)
    return out


# ─── extra artifacts ───────────────────────────────────────────────────────────────────────


def _title_keys(title: str) -> list[str]:
    """`CEO (Chief Executive Officer)` -> [`ceo`, `chief executive officer`]."""
    title = " ".join(title.split())
    match = _TRAILING_PAREN.match(title)
    parts = [match.group(1), match.group(2)] if match else [title]
    return [key for key in (normalize_surface(part) for part in parts) if key]


def build_titles(raw: Raw, generic: Iterable[str] = ()) -> dict[str, Any]:
    """Normalised title -> {soc, family, job_zone}. A primary O*NET title wins; otherwise the
    title's candidate occupations vote per SOC major group (ties: the smaller group code) and the
    smallest code of the winning group is kept. A generic title ("intern", "student", "engineer":
    curated `generic_titles`) names no occupation and is left out (DECISIONS D-26)."""
    left_out = frozenset(generic)
    primary: dict[str, set[str]] = defaultdict(set)
    others: dict[str, set[str]] = defaultdict(set)
    for row in raw.occupations:
        for key in _title_keys(row["Title"]):
            primary[key].add(row["O*NET-SOC Code"])
    for row in raw.alternate_titles:
        for column in ("Alternate Title", "Short Title"):
            value = row.get(column, "").strip()
            if value and value.lower() != "n/a":
                for key in _title_keys(value):
                    others[key].add(row["O*NET-SOC Code"])
    for row in raw.reported_titles:
        for key in _title_keys(row["Reported Job Title"]):
            others[key].add(row["O*NET-SOC Code"])
    titles = {}
    for key in sorted((set(primary) | set(others)) - left_out):
        candidates = primary.get(key) or others[key]
        votes: dict[str, int] = defaultdict(int)
        for soc in candidates:
            votes[soc[:2]] += 1
        family = min(votes, key=lambda group: (-votes[group], group))
        soc = min(code for code in candidates if code[:2] == family)
        titles[key] = {"soc": soc, "family": SOC_MAJOR_GROUPS.get(family, ""), "job_zone": raw.job_zones.get(soc)}
    return {"format": 1, "titles": titles}


@dataclass
class OccupationSkills:
    """Per O*NET-SOC code: ids of its hot or in-demand technologies, and of its top knowledge areas."""

    technologies: dict[str, set[str]]
    knowledge: dict[str, set[str]]

    def of(self, socs: Iterable[str]) -> tuple[set[str], set[str]]:
        socs = list(socs)
        return (set().union(*(self.technologies.get(soc, set()) for soc in socs)),
                set().union(*(self.knowledge.get(soc, set()) for soc in socs)))


def _occupation_skills(raw: Raw, assembler: Assembler, knowledge_map: Mapping[str, str]) -> OccupationSkills:
    technologies: dict[str, set[str]] = defaultdict(set)
    for row in raw.tech:
        if row.get("Hot Technology") == "Y" or row.get("In Demand") == "Y":
            node_id = assembler.example_map.get(row["Example"].strip())
            if node_id is not None and node_id in assembler.final_ids:
                technologies[row["O*NET-SOC Code"]].add(node_id)
    knowledge: dict[str, set[str]] = defaultdict(set)
    ratings: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
    for row in raw.knowledge:
        if row.get("Scale ID") != "IM":
            continue
        try:
            value = float(row["Data Value"])
        except ValueError:
            continue
        if value >= KNOWLEDGE_MIN_IMPORTANCE:
            ratings[row["O*NET-SOC Code"]].append((-value, row["Element ID"], row["Element Name"]))
    for soc, entries in ratings.items():
        for _, _, name in sorted(entries)[:KNOWLEDGE_TOP]:
            node_id = knowledge_map.get(name)
            if node_id is not None:
                knowledge[soc].add(node_id)
    return OccupationSkills(dict(technologies), dict(knowledge))


def build_mos(raw: Raw, occupation_skills: OccupationSkills) -> dict[str, Any]:
    """Active primary military occupation codes that map to at least one O*NET-SOC occupation.

    A code's `skill_ids` come only from the occupations it maps to (DECISIONS D-29): their hot or
    in-demand technologies and top knowledge areas, never fields of study (`field.*`) and never a
    knowledge area that more than MOS_COMMON_KNOWLEDGE_SHARE of all codes would get (English, ...).
    An entry left without skills carries a `note` saying why."""
    per_code: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)  # code -> branch -> entry
    mpc_rank = {"E": 0, "W": 1, "O": 2}
    for row in raw.military:
        branch = MILITARY_BRANCHES.get(row.get("SVC", ""))
        if branch is None or row.get("STATUS") != "A" or row.get("MPC") not in mpc_rank:
            continue
        socs = [row.get(f"ONET{i}", "").strip() for i in range(1, 5)]
        socs = [soc for soc in socs if re.fullmatch(r"\d\d-\d{4}\.\d\d", soc)]
        if not socs:
            continue
        code = row["MOC"].strip()
        title = " ".join(row["MOC_TITLE"].split())
        entry = per_code[code].get(branch)
        rank = (mpc_rank[row["MPC"]], title)
        if entry is None or rank < entry["rank"]:
            per_code[code][branch] = {"rank": rank, "title": title,
                                      "soc": set(socs) | (entry["soc"] if entry else set())}
        else:
            entry["soc"].update(socs)
        afsc = _AFSC.fullmatch(code) if branch in ("air_force", "space_force") and row["MPC"] == "E" else None
        if afsc:
            generic = f"{afsc.group(1)}X{afsc.group(3)}"
            plain = _AFSC_LEVEL_WORDS.sub("", title).strip(" ,") or title
            entry = per_code[generic].get(branch)
            rank = (int(afsc.group(2)) != 5, len(code), plain)  # the 5-level title reads best
            if entry is None or rank < entry["rank"]:
                per_code[generic][branch] = {"rank": rank, "title": plain,
                                             "soc": set(socs) | (entry["soc"] if entry else set())}
            else:
                entry["soc"].update(socs)

    def areas(socs: Iterable[str]) -> set[str]:
        return {skill for skill in occupation_skills.of(socs)[1] if not skill.startswith("field.")}

    entries = {code: sorted(per_code[code], key=BRANCH_ORDER.index) for code in sorted(per_code)}
    counts: dict[str, int] = defaultdict(int)  # knowledge area -> codes that would get it
    for code, branches in entries.items():
        for skill in set().union(*(areas(per_code[code][branch]["soc"]) for branch in branches)):
            counts[skill] += 1
    common = {skill for skill, count in counts.items() if count > MOS_COMMON_KNOWLEDGE_SHARE * len(entries)}

    def describe(branch: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        socs = sorted(entry["soc"])
        technologies = {skill for skill in occupation_skills.of(socs)[0] if not skill.startswith("field.")}
        value = {"branch": branch, "title": entry["title"], "soc": socs,
                 "skill_ids": sorted(technologies | (areas(socs) - common))}
        if not value["skill_ids"]:
            value["note"] = _mos_note(socs, occupation_skills)
        return value

    codes = {}
    for code, branches in entries.items():
        first = describe(branches[0], per_code[code][branches[0]])
        value = {**first, "branches": branches}
        alternatives = []
        for branch in branches[1:]:
            other = describe(branch, per_code[code][branch])
            if (other["title"].casefold(), other["soc"]) != (first["title"].casefold(), first["soc"]):
                alternatives.append(other)
        if alternatives:
            value["alternatives"] = alternatives
        codes[code] = value
    return {"format": 1, "codes": codes, "common_knowledge_left_out": sorted(common)}


def _mos_note(socs: Sequence[str], occupation_skills: OccupationSkills) -> str:
    """Why an entry has no skill ids (never filled from sibling occupations: D-29)."""
    listed = ", ".join(socs)
    if any(occupation_skills.of(socs)):
        return f"{listed}: only fields of study or knowledge areas most codes share (left out)"
    if all(soc.startswith("55-") for soc in socs):
        return f"{listed}: military-specific occupation(s); O*NET publishes no skill data for them"
    return f"{listed}: O*NET publishes no skill data for the occupation(s) (e.g. \"All Other\" catch-alls)"


def build_naics(raw: Raw) -> dict[str, Any]:
    codes = {}
    for code, title in raw.naics:
        span = re.fullmatch(r"(\d\d)-(\d\d)", code)
        if span:
            for value in range(int(span.group(1)), int(span.group(2)) + 1):
                codes[f"{value:02d}"] = title
        elif re.fullmatch(r"\d{2,6}", code):
            codes[code] = title
    return {"format": 1, "codes": dict(sorted(codes.items()))}


# ─── sources ───────────────────────────────────────────────────────────────────────────────

_ATTRIBUTION = {
    "esco_en": "ESCO (European Skills, Competences, Qualifications and Occupations), European Commission. "
               "Reused under the Commission's reuse policy (Decision 2011/833/EU), compatible with CC BY 4.0.",
    "esco_fr": "As ESCO English (French labels of the same concepts).",
    "onet_db": "O*NET 30.0 Database by the U.S. Department of Labor, Employment and Training Administration "
               "(USDOL/ETA). Used under the CC BY 4.0 license. O*NET is a trademark of USDOL/ETA. This "
               "taxonomy is derived from O*NET data; USDOL/ETA has not approved, endorsed or tested it.",
    "onet_military": "O*NET Resource Center military crosswalk (MOC data from the Defense Manpower Data "
                     "Center), CC BY 4.0, USDOL/ETA.",
    "naics_2022": "North American Industry Classification System 2022, U.S. Census Bureau (public domain).",
}


def manifest_sources(raw: Raw, curated_path: Path, edges_path: Path) -> dict[str, str]:
    sources = {}
    for name, entry in sorted(raw.record.get("downloads", {}).items()):
        if entry.get("error"):
            sources[name] = f"NOT FETCHED: {entry.get('title', name)} ({entry['error']})"
        else:
            sources[name] = f"{entry.get('title', name)}; {entry.get('url', '')}; sha256 {entry.get('sha256', '')}"
    sources["curated"] = (f"config/{curated_path.name} sha256 {_text_sha256(curated_path)}; "
                          f"config/{edges_path.name} sha256 {_text_sha256(edges_path)}")
    return sources


def _mos_empty_summary(mos: Mapping[str, Any]) -> str:
    """How many codes have no skill ids, by reason (the `note` of their primary entry)."""
    reasons = {"military-specific": 0, "no data": 0, "left out": 0}
    for value in mos["codes"].values():
        note = value.get("note")
        if note is None:
            continue
        key = ("military-specific" if "military-specific" in note
               else "left out" if "left out" in note else "no data")
        reasons[key] += 1
    empty = sum(reasons.values())
    return (f"Codes without skill ids: {empty:,} of {len(mos['codes']):,} codes have none:"
            f" {reasons['military-specific']:,} map only to military-specific occupations (SOC 55-xxxx),"
            f" {reasons['no data']:,} to civilian occupations O*NET publishes no skill data for (\"All Other\""
            f" catch-alls and the like), {reasons['left out']:,} to occupations whose only data was left out.")


def sources_markdown(raw: Raw, nodes: Sequence[SkillNode], report: Report, extras: Mapping[str, Any],
                     edges: int, ambiguous: int) -> str:
    lines = [
        "# Skill taxonomy sources",
        "",
        "Generated by `scripts/build_taxonomy.py` from the downloads `scripts/fetch_sources.py` records in",
        "`data/raw/fetch_record.json` (git-ignored). Do not edit by hand: rebuild. Hashes are of the files",
        "as downloaded.",
        "",
        "| Source | Version / file | Fetched | sha256 |",
        "|---|---|---|---|",
    ]
    downloads = raw.record.get("downloads", {})
    for name in ("esco_en", "esco_fr", "onet_db", "onet_military", "naics_2022"):
        entry = downloads.get(name)
        if not entry:
            lines.append(f"| {name} | NOT FETCHED | - | - |")
        elif entry.get("error"):
            lines.append(f"| {name} | NOT FETCHED: {entry['error']} | - | - |")
        else:
            lines.append(f"| {name} | {entry.get('title', '')} ({entry.get('url', '')}) | "
                         f"{entry.get('fetched', '')} | `{entry.get('sha256', '')}` |")
    lines += ["", "## Licences and attribution", ""]
    for name, text in _ATTRIBUTION.items():
        lines.append(f"- **{name}**: {text}")
    lines += [
        "- **Tailrd curated tables**: `config/curated_taxonomy.yaml` and `config/curated_edges.yaml`, which",
        "  carry the content of `backend/services/structured_extraction.py:_SKILL_SYNONYMS` and",
        "  `backend/services/local_match.py:IMPLIES/_DISPLAY` (DECISIONS D-20) plus hand-written nodes.",
        "- SOC 2018 major group titles (U.S. Bureau of Labor Statistics, public domain) are a table in the",
        "  build script.",
        "",
        "## What was used",
        "",
        "- ESCO: skills, skill groups (pillar roots excluded), broader relations, the transversal skills",
        f"  collection. French labels: {'yes' if raw.french else 'NO (not fetched; English only)'}.",
        "  Occupations: their descriptions only, as part of the common-word corpus (below); the",
        "  occupation-skill relations are downloaded but not used.",
        "- O*NET: Technology Skills, Tools Used (examples and their UNSPSC commodity classes), Occupation Data,",
        "  Alternate Titles, Sample of Reported Titles, Job Zones, Knowledge; Task Statements only as part of",
        "  the common-word corpus.",
        "- Military crosswalk: " + ("active primary codes (Army, Marine Corps, Navy, Air Force, Space Force,"
                                   " Coast Guard) mapped to O*NET-SOC; enlisted AFSCs also under their skill-level-X"
                                   " form (3D052 -> 3D0X2)." if raw.military else "NOT FETCHED: mos_crosswalk.json"
                                   " is empty."),
        "- NAICS 2022: 2-6 digit codes; the sector ranges 31-33, 44-45 and 48-49 are expanded to each code.",
        "- IDF is empty and related edges are the curated ones only: Task 2.3 computes both from the corpus.",
        "",
        "## Rules worth knowing when reading the artifacts",
        "",
        "- Aliases (DECISIONS D-26): an ESCO concept merged into a curated node brings its preferred labels",
        "  only, never its alternative or hidden labels; a one-word French preferred label is kept only when",
        "  it is the English label. ESCO programming languages (labelled \"(computer programming)\" or under",
        "  computer programming) are TECHNOLOGY.",
        "- titles.json: a primary O*NET title maps to its occupation; an alternate or reported title claimed",
        "  by several occupations goes to the SOC major group most of them share (ties: the smaller group",
        "  code), then to that group's smallest code. Trailing parentheticals give two keys",
        "  (`CEO (Chief Executive Officer)` -> `ceo`, `chief executive officer`). Job zone null: O*NET has none.",
        "  Generic titles (curated `generic_titles`: intern, co-op, student, trainee, apprentice, volunteer,",
        "  engineer, analyst, ...) name no occupation and are left out.",
        "- mos_crosswalk.json: one entry per code; `branches` lists every service using it, `branch` is the",
        "  first in Army, Marine Corps, Navy, Air Force, Space Force, Coast Guard order, and `alternatives`",
        "  holds a service whose use of the code maps elsewhere. `skill_ids` (DECISIONS D-29) come only from",
        "  the occupations a code maps to: their hot or in-demand technologies and top five knowledge areas",
        "  (importance >= 3), never fields of study and never a knowledge area more than "
        f"{MOS_COMMON_KNOWLEDGE_SHARE:.0%}",
        "  of all codes would get (`common_knowledge_left_out`). An entry without skills carries a `note`.",
        "- " + _mos_empty_summary(extras["mos_crosswalk.json"]),
        f"- Common words: lower-case words used at least {COMMON_WORD_MIN_COUNT} times in the ESCO skill and",
        "  occupation descriptions and the O*NET occupation descriptions and task statements. A shortened",
        "  source name (vendor-free, without \"software\", a bare label, an acronym half) that is one such",
        "  word is not an alias (`Microsoft Paint` keeps no `Paint`).",
        "- resolver.json (DECISIONS D-26, D-27): `ambiguous` holds the curated keys that need their casing",
        "  (a skills list waives it); `cased` holds, automatically, every one-word name with a capital on a",
        "  node that is not curated (O*NET products and tools, ESCO languages, acronyms), cased as written",
        "  everywhere: lower-case text never resolves to them and the fuzzy fallback never targets them.",
        "  `blocked_phrases` (\"American Express\", \"Sous Chef\", \"Building C\") consume their words in",
        "  extraction without a mention.",
        "",
        "## Counts",
        "",
        "| Kind | Nodes |",
        "|---|---|",
    ]
    by_kind: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    alias_by_source: dict[str, int] = defaultdict(int)
    for node in nodes:
        by_kind[node.kind.value] += 1
        by_source[node.source] += 1
        alias_by_source[node.source] += len(node.aliases)
    for kind in SkillKind:
        lines.append(f"| {kind.value} | {by_kind.get(kind.value, 0):,} |")
    lines += ["", "| Source | Nodes | Aliases |", "|---|---|---|"]
    for source in sorted(by_source):
        lines.append(f"| {source} | {by_source[source]:,} | {alias_by_source[source]:,} |")
    lines += [
        "",
        f"- Total: {len(nodes):,} nodes, {sum(len(n.aliases) for n in nodes):,} aliases; "
        f"{edges:,} curated related edges; {ambiguous:,} curated ambiguous keys and "
        f"{len(report.auto_ambiguous):,} automatic cased keys (one-word names with a capital on nodes that are "
        f"not curated, cased as written); {len(extras['resolver.json']['blocked_phrases']):,} blocked phrases.",
        f"- Merged into curated nodes: {len(report.merges):,} source concepts; dropped nodes: "
        f"{len(report.dropped_nodes):,}; keys dropped as ambiguous between nodes: {len(report.ambiguous_keys):,};"
        f" label ties left to the resolver: {len(report.label_ties):,}.",
        f"- titles.json: {len(extras['titles.json']['titles']):,} titles; mos_crosswalk.json:"
        f" {len(extras['mos_crosswalk.json']['codes']):,} codes; naics.json:"
        f" {len(extras['naics.json']['codes']):,} codes.",
        "",
    ]
    return "\n".join(lines)


# ─── build ─────────────────────────────────────────────────────────────────────────────────


def build(
    raw_dir: Path,
    out_dir: Path,
    *,
    curated_path: Path = DEFAULT_CURATED,
    edges_path: Path = DEFAULT_EDGES,
    required_ids: Iterable[str] = (),
) -> Report:
    started = time.perf_counter()
    report = Report()
    curated = load_curated(curated_path)
    curated_edges = load_curated_edges(edges_path)
    raw = read_raw(Path(raw_dir))

    assembler = Assembler(curated, report, raw.common_words)
    assembler.add_curated()
    assembler.add_esco(raw)
    assembler.add_onet(raw)
    nodes = assembler.finish()
    ids = {node.id for node in nodes}
    assembler.final_ids = ids

    missing = sorted(set(required_ids) - ids)
    if missing:
        raise BuildError(f"required credential ids missing from the taxonomy: {missing}")
    for a, b, _ in curated_edges:
        for node_id in (a, b):
            if node_id not in ids:
                raise BuildError(f"curated edge endpoint {node_id} is not a node")
    for name, node_id in curated.onet_knowledge.items():
        if node_id not in ids:
            raise BuildError(f"onet_knowledge maps {name!r} to {node_id}, which is not a node")
    index_keys = set()
    for node in nodes:
        for surface in (node.label, *node.aliases):
            index_keys.update(_keys(surface))
    unused = sorted(key for key in curated.ambiguous if key not in index_keys)
    if unused:
        raise BuildError(f"ambiguous keys that no node claims: {unused}")

    curated_map: dict[str, dict[str, float]] = defaultdict(dict)
    for a, b, weight in curated_edges:
        curated_map[a][b] = weight
    edges = build_edges({}, {}, 1, threshold=1.0, min_cooccurrence=1, max_out_degree=0, curated=curated_map)

    report.auto_ambiguous = auto_ambiguous(nodes, curated.ambiguous)
    resolver_settings = {
        "format": 1,
        "ambiguous": dict(sorted(curated.ambiguous.items())),
        "cased": dict(sorted(report.auto_ambiguous.items())),
        "blocked_phrases": list(curated.blocked_phrases),
    }
    extras = {
        "resolver.json": resolver_settings,
        "titles.json": build_titles(raw, curated.generic_titles),
        "mos_crosswalk.json": build_mos(raw, _occupation_skills(raw, assembler, curated.onet_knowledge)),
        "naics.json": build_naics(raw),
    }
    out_dir = Path(out_dir)
    write_artifacts(out_dir, nodes, {}, edges, n_postings=0, sources=manifest_sources(raw, curated_path, edges_path),
                    compress=True, extra=extras)
    text = sources_markdown(raw, nodes, report, extras, sum(len(t) for t in edges.values()) // 2,
                            len(curated.ambiguous))
    (out_dir / "SOURCES.md").write_bytes(text.encode("utf-8"))
    (out_dir / ".gitattributes").write_bytes(GITATTRIBUTES.encode("utf-8"))
    report.counts = {"nodes": len(nodes), "aliases": sum(len(n.aliases) for n in nodes)}
    report.seconds = time.perf_counter() - started
    return report


def _write_review(report: Report, review_dir: Path) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)

    def dump(name: str, rows: Iterable[Sequence[Any]]) -> None:
        text = "".join("\t".join(str(x) for x in row) + "\n" for row in rows)
        (review_dir / name).write_text(text, encoding="utf-8")

    dump("merges.tsv", sorted(report.merges, key=lambda r: (r[2], r[0], r[1])))
    dump("dropped_nodes.tsv", sorted(report.dropped_nodes))
    dump("ambiguous_keys.tsv", ((key, " ".join(ids)) for key, ids in report.ambiguous_keys))
    dump("label_ties.tsv", ((key, " ".join(ids)) for key, ids in report.label_ties))
    dump("derived_single_words.tsv", sorted(report.derived_single_words, key=lambda r: r[0].casefold()))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--curated", type=Path, default=DEFAULT_CURATED)
    parser.add_argument("--edges", type=Path, default=DEFAULT_EDGES)
    parser.add_argument("--review", type=Path, default=None, help="write curation review TSVs here")
    parser.add_argument("--required", nargs="*", default=None, metavar="ID",
                        help="credential ids the taxonomy must hold (default: the professional credential ids "
                             "of config/gates.yaml; give the flag alone for none)")
    args = parser.parse_args(argv)
    required = args.required
    if required is None:
        gates = yaml.safe_load(DEFAULT_GATES.read_text(encoding="utf-8"))
        required = gates["license"]["professional_credential_ids"]
    report = build(args.raw, args.out, curated_path=args.curated, edges_path=args.edges, required_ids=required)
    if args.review is not None:
        _write_review(report, args.review)
    print(f"built {report.counts['nodes']:,} nodes, {report.counts['aliases']:,} aliases into {args.out} "
          f"in {report.seconds:.1f} s; merges {len(report.merges):,}, dropped nodes {len(report.dropped_nodes):,}, "
          f"ambiguous keys {len(report.ambiguous_keys):,}, label ties {len(report.label_ties):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
