"""
Parity check: backend.services.profile_facts against the REAL TypeScript it ports.

``backend/services/profile_facts.py`` reads a saved profile the way
``chrome-extension/src/content/profileFacts.ts`` (and its tables in ``geo.ts``) does, and the two
have to keep saying the same thing. This module is how they are kept in step. It generates a seeded
corpus of inputs, asks the TypeScript what it makes of each one (Node >= 22.6 imports the two files
IN PLACE and strips the types itself: no copy, no build), asks the port, and compares every fact,
confidence and ``source`` rule. The pytest entry point is ``backend/tests/test_profile_facts_parity.py``;
it skips when Node >= 22.6 is not installed.

Re-check, deeper than the pytest default (``DEFAULT_PER_KIND`` cases of each of five kinds)::

    python -m backend.tests.tools.profile_facts_parity --seed 7 --cases 20000
    python -m backend.tests.tools.profile_facts_parity --tables           # only the geo tables

Ask the TypeScript a question (this is how the expected values in test_profile_facts.py were made)::

    >>> from backend.tests.tools.profile_facts_parity import run_reference
    >>> run_reference([{"kind": "polarity", "text": "N/A"}])
    [{'ok': False}]

Case kinds: ``polarity``, ``countriesIn`` and ``parseAddress`` take ``text``; ``location`` takes
``profile`` (a dict of the extension's camelCase fields); ``workAuth`` takes ``profile`` and
``queries`` (``[[country code | None, residence | None], ...]``) for authorizedIn and
needsSponsorshipIn.

When it fails, the extension and the port disagree about a case printed in the failure. Decide
which one is right and change it (and, for the port, backend/tests/test_profile_facts.py) in one
commit, so both keep saying the same thing. A case on which the TypeScript itself throws is
reported as a mismatch too: keep such inputs out of the generator below.

The generated text is built from ``chr()`` rather than escape sequences so that this file stays
plain ASCII: it deliberately includes the characters JavaScript and Python regular expressions
disagree about (non-breaking space, byte-order mark, U+0085, accented letters next to tokens,
full-width digits, ...).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from backend.services import profile_facts as port

REPO_ROOT = Path(__file__).resolve().parents[3]
#: Where the extension's profileFacts.ts and geo.ts live. They are imported from here, in place.
CONTENT_DIR = REPO_ROOT / "chrome-extension" / "src" / "content"
NODE_DRIVER = Path(__file__).with_name("ts_profile_facts_reference.mjs")
#: Type stripping (`--experimental-strip-types`) arrived in Node 22.6.
MIN_NODE = (22, 6, 0)

DEFAULT_SEED = 20261009
DEFAULT_PER_KIND = 3000
KINDS = ("polarity", "countriesIn", "parseAddress", "location", "workAuth")

#: Rule sources (`source|confidence`, and `basis:` for per-country bases) the default corpus must reach.
EXPECTED_RULE_SOURCES = frozenset(
    {
        "address:city|high", "address:country|high", "address:parsed-street|high", "address:postal|high",
        "address:region|high", "city-hint|medium", "location:city|high", "location:city|medium",
        "location:postal|high", "location:region|high", "profile:addressCity|high",
        "profile:addressState|high", "profile:addressStreet|high", "profile:country|high",
        "profile:postalCode|high",
        "basis:citizen", "basis:denied", "basis:permanent_resident", "basis:stated", "basis:statement",
        "basis:student", "basis:work_permit",
        "work-auth:citizen|high", "work-auth:denied|high", "work-auth:free-movement|high",
        "work-auth:permanent_resident|high", "work-auth:stated|high", "work-auth:statement|high",
        "work-auth:student|high", "work-auth:unscoped|high", "work-auth:unscoped-residence|high",
        "work-auth:work_permit|high",
        "sponsorship:citizen|high", "sponsorship:citizen-unscoped|medium",
        "sponsorship:free-movement|high", "sponsorship:not-authorized-denied|high",
        "sponsorship:not-authorized-stated|high", "sponsorship:permanent_resident|high",
        "sponsorship:stated|high", "sponsorship:stated-multi-country|medium",
        "sponsorship:stated-unscoped|high", "sponsorship:stated-unscoped-not-everywhere|medium",
        "sponsorship:work_permit|high",
    }
)


# ---------------------------------------------------------------------------------------------
# Asking the TypeScript
# ---------------------------------------------------------------------------------------------


def _node_version(node: str) -> Optional[tuple[int, int, int]]:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", out)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def unavailable_reason() -> Optional[str]:
    """Why the TypeScript cannot be run here (for a pytest skip message), or None when it can."""
    node = shutil.which("node")
    if node is None:
        return "Node.js is not installed: the profile-facts parity check needs Node >= 22.6"
    version = _node_version(node)
    if version is None or version < MIN_NODE:
        shown = ".".join(map(str, version)) if version else "an unknown version"
        return f"Node {shown} is too old: the profile-facts parity check needs Node >= 22.6 (type stripping)"
    for name in ("profileFacts.ts", "geo.ts"):
        if not (CONTENT_DIR / name).is_file():
            return f"{CONTENT_DIR / name} not found: the profile-facts parity check reads the extension in place"
    return None


def _run_node(cases: list[dict]) -> dict:
    reason = unavailable_reason()
    if reason is not None:
        raise RuntimeError(reason)
    node = shutil.which("node")
    with tempfile.TemporaryDirectory() as tmp:
        corpus = Path(tmp) / "corpus.json"
        corpus.write_text(json.dumps(cases), encoding="utf-8")
        proc = subprocess.run(
            [node, "--experimental-strip-types", "--no-warnings", str(NODE_DRIVER), str(corpus), str(CONTENT_DIR)],
            capture_output=True, text=True, encoding="utf-8", timeout=600, check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError("the TypeScript reference run failed:\n" + proc.stderr[-4000:])
    return json.loads(proc.stdout)


def run_reference(cases: list[dict]) -> list[dict]:
    """What the extension's own code returns for each case: ``{"ok": value}`` or ``{"error": message}``."""
    return _run_node(cases)["results"]


def reference_tables() -> dict:
    """The extension's geo tables: ``{name: {"value": ...} | {"error": message}}``."""
    return _run_node([])["tables"]


# ---------------------------------------------------------------------------------------------
# Asking the port (same JSON shapes as ts_profile_facts_reference.mjs)
# ---------------------------------------------------------------------------------------------


def _country(c: Optional[port.Country]) -> Optional[dict]:
    return None if c is None else {"code": c.code, "name": c.name}


def _region(r: Optional[port.Region]) -> Optional[dict]:
    return None if r is None else {"code": r.code, "name": r.name, "country": r.country}


def _fact(f: Optional[port.Fact], kind: Optional[str] = None) -> Optional[dict]:
    if f is None:
        return None
    value = f.value
    if kind == "country":
        value = _country(value)
    elif kind == "region":
        value = _region(value)
    return {"value": value, "confidence": f.confidence, "source": f.source}


def _parsed(a: port.ParsedAddress) -> dict:
    out: dict[str, Any] = {}
    if a.street is not None:
        out["street"] = a.street
    if a.city is not None:
        out["city"] = a.city
    if a.region is not None:
        out["region"] = _region(a.region)
    if a.postal is not None:
        out["postal"] = {"code": a.postal.code, "country": a.postal.country}
    if a.country is not None:
        out["country"] = _country(a.country)
    return out


def python_result(case: dict) -> Any:
    """What the port returns for a case, in the shape the TypeScript driver prints."""
    kind = case["kind"]
    if kind == "polarity":
        return port.polarity_of(case["text"])
    if kind == "countriesIn":
        return [c.code for c in port.countries_in(case["text"])]
    if kind == "parseAddress":
        return _parsed(port.parse_address(case["text"]))
    if kind == "location":
        f = port.location_facts(case["profile"])
        return {
            "street": _fact(f.street), "city": _fact(f.city), "region": _fact(f.region, "region"),
            "postalCode": _fact(f.postal_code), "country": _fact(f.country, "country"),
        }
    if kind == "workAuth":
        a = port.work_auth_facts(case["profile"])
        return {
            "byCountry": [
                [code, {"authorized": v.authorized, "needsSponsorship": v.needs_sponsorship, "basis": v.basis}]
                for code, v in a.by_country.items()
            ],
            "statedSponsorship": a.stated_sponsorship,
            "unscopedAuthorized": a.unscoped_authorized,
            "queries": [
                {
                    "q": q,
                    "authorized": _fact(port.authorized_in(a, q[0], q[1])),
                    "sponsorship": _fact(port.needs_sponsorship_in(a, q[0], q[1])),
                }
                for q in case["queries"]
            ],
        }
    raise ValueError(f"unknown case kind {kind!r}")


# ---------------------------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------------------------


def _brief(case: dict, limit: int = 320) -> str:
    text = json.dumps(case, ensure_ascii=True)  # ASCII so odd characters show as escapes
    return text if len(text) <= limit else text[:limit] + "..."


def compare(cases: list[dict], reference: list[dict]) -> list[str]:
    """One message per case on which the port and the TypeScript disagree (empty when they agree)."""
    mismatches: list[str] = []
    for index, (case, ref) in enumerate(zip(cases, reference)):
        label = f"#{index} {_brief(case)}"
        if "error" in ref:
            mismatches.append(f"{label}\n   the TypeScript raised: {ref['error']}")
            continue
        try:
            mine = python_result(case)
        except Exception as error:  # noqa: BLE001 - any failure of the port is a mismatch to report
            mismatches.append(f"{label}\n   the port raised: {error!r}")
            continue
        if mine != ref["ok"]:
            mismatches.append(
                f"{label}\n   TypeScript: {json.dumps(ref['ok'], ensure_ascii=True)[:600]}"
                f"\n   port:       {json.dumps(mine, ensure_ascii=True)[:600]}"
            )
    return mismatches


def rule_sources_reached(cases: list[dict], reference: list[dict]) -> set[str]:
    """The rules (``source|confidence``, ``basis:...``) the TypeScript itself fired over the corpus."""
    reached: set[str] = set()
    for case, ref in zip(cases, reference):
        if "ok" not in ref:
            continue
        out = ref["ok"]
        if case["kind"] == "location":
            reached.update(f"{f['source']}|{f['confidence']}" for f in out.values() if f)
        elif case["kind"] == "workAuth":
            for q in out["queries"]:
                reached.update(f"{f['source']}|{f['confidence']}" for f in (q["authorized"], q["sponsorship"]) if f)
            reached.update("basis:" + v["basis"] for _, v in out["byCountry"])
    return reached


def table_mismatches() -> list[str]:
    """Row-for-row differences between the port's geo tables and the extension's (empty when identical)."""
    ref = reference_tables()
    problems: list[str] = []

    def reference_value(name: str) -> Any:
        if "error" in ref[name]:
            problems.append(f"{name}: {ref[name]['error']}")
            return None
        return ref[name]["value"]

    def countries(rows: Any) -> list[dict]:
        return [{"code": c.code, "name": c.name, "continent": c.continent, "aliases": list(c.aliases)} for c in rows]

    def regions(rows: Any) -> list[dict]:
        return [
            {
                "code": r.code, "name": r.name, "country": r.country,
                **({"aliases": list(r.aliases)} if r.aliases else {}),
            }
            for r in rows
        ]

    def without_none(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in row.items() if v is not None} for row in rows]

    for name, mine in (
        ("COUNTRIES", countries(port.COUNTRIES)),
        ("CA_PROVINCES", regions(port.CA_PROVINCES)),
        ("US_STATES", regions(port.US_STATES)),
    ):
        theirs = reference_value(name)
        if theirs is not None and mine != without_none(theirs):
            problems.append(f"{name} differs from the extension's")
    cities = reference_value("CITY_COUNTRY")
    if cities is not None and (dict(port._CITY_COUNTRY) != cities or list(port._CITY_COUNTRY) != list(cities)):
        problems.append("CITY_COUNTRY (the city hints) differs from the extension's")
    movement = reference_value("FREE_MOVEMENT")
    if movement is not None and (set(port.FREE_MOVEMENT) != set(movement) or len(movement) != len(set(movement))):
        problems.append("FREE_MOVEMENT differs from the extension's")
    return problems


# ---------------------------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------------------------

# Characters JavaScript and Python regular expressions treat differently (see profile_facts._js_regex).
NBSP = chr(0xA0)  # JS whitespace; what pasted text carries
BOM = chr(0xFEFF)  # JS whitespace, not Python's
NEL = chr(0x85)  # Python whitespace, not JS's
LINE_SEPARATOR = chr(0x2028)
EM_SPACE = chr(0x2003)
IDEOGRAPHIC_SPACE = chr(0x3000)
ZERO_WIDTH_SPACE = chr(0x200B)  # whitespace in neither
UNIT_SEPARATOR = chr(0x1F)  # Python whitespace, not JS's
E_ACUTE, E_ACUTE_CAPITAL, E_GRAVE, A_GRAVE, A_TILDE = chr(0xE9), chr(0xC9), chr(0xE8), chr(0xE0), chr(0xE3)
C_CEDILLA, O_UMLAUT, U_UMLAUT, SHARP_S = chr(0xE7), chr(0xF6), chr(0xFC), chr(0xDF)
DOTTED_CAPITAL_I, DOTLESS_I, LONG_S, KELVIN_SIGN = chr(0x130), chr(0x131), chr(0x17F), chr(0x212A)
FULLWIDTH_DIGITS = "".join(chr(0xFF10 + d) for d in (1, 2, 3, 4, 5))
ARABIC_INDIC_DIGITS = "".join(chr(0x660 + d) for d in (1, 2, 3, 4, 5))

WORDS = """authorized authorised eligible entitled permitted allowed legally able right to work in the of and or
with for a an I am is not no never yes y n N/A none maybe sure applicable citizen citizenship national
permanent resident green card landed immigrant lawful PR student visa study permit international F-1 F1 J-1 M-1
H-1B H1B H-4 L-1 L-1A E-3 O-1 TN open work post-graduation PGWP EAD employment authorization authorisation OPT
CPT STEM Canada Canadian US U.S. USA U.S.A. us America American North South Latin Central United States Kingdom
UK U.K. British Britain England Scotland German Germany French France Indian India Mexican Mexico Iranian
Austria Austrian Swiss Switzerland Irish Ireland Spanish Spain EU Europe Citoyen canadien Quebec Ontario will do
does would need require required needed sponsorship sponsor don't won't doesn't longer true false oui non si
Chinese China Japan Korea Korean Australia Brazil Nigeria Kenya Egypt Morocco Ghana Polish Poland Dutch Holland
Greek Turkish Israeli Pakistani now future currently status can legally without holder card valid through until
June 2028 2026 (US) (Canada) (EU) / - , ; . : ! ?""".split() + [
    "North America", "north america", "Latin America", "Central America", "South America", "south american",
    "central american", "in US", "the US", "for us", "in the U.S.", "work for us", "in America", "Americas",
    "H-1B visa", "green-card", "U.S. citizen", "Canadian citizen", "permanent resident of Canada",
    "Authorized to work in", "not authorized", "no longer", "do not require",
    # Negation scope and N/A (owner decisions 2026-10-13).
    "but not in the US", "but not the", "except the US", "excluding", "other than", "outside of", "only in",
    "not in the US and the UK", "and authorized", "not a US citizen", "not requiring sponsorship", "NA", "n/a",
    "N.A.", "not applicable", "does not apply", "however", "whereas", "yet",
    # Decide-b review round 1: dotted spellings, other negations, uncertainty, sponsorship clauses.
    "U.S.", "U.S", "U.S.A.", "u.s.", "U. S.", "U.K.", "not in the U.S.", "except the U.S.", "not sure about",
    "unsure", "not certain", "not yet", "not currently", "not only", "no US work authorization", "without",
    "cannot", "can't", "can’t", "unable to work", "ineligible", "unauthorized", "don't have", "do not have",
    "status", "work authorization: no", "US not authorized", "US no", ": no", "I would require sponsorship",
    "would need", "requires sponsorship", "not Canadian", "Plan B. I am", "unknown", "TBD", "but will",
    "in the future", "later", "pending", "in progress", "applied for", "US requires sponsorship",
    # Per-clause statuses: segment joins, visa eligibility, country lists, dotted abbreviations mid-sentence.
    "via", "plus", "&", "TN eligible", "eligible for H-1B", "OPT eligible", "Canadian and American citizen",
    "US resident", "U.S. Citizen", "U.S. I am", "U.S.A. citizen", "Citizen of Canada", "both", "Permanent",
    "I am", "Work",
    # Binding model (decide-b round 3): labelled answers, marks, residence words, visa intent.
    "Yes for", "no for", "?", chr(0x2705), chr(0x274C), chr(10), "|", " / ", " - ", "living in", "residing in", "based",
    "US-based", "dual citizen", "PR card", "GC", "perm res", "only", "Canada: Yes", "US: No", "applying for",
    "waiting on", "starting", "would need sponsorship", "need visa", "for us", "us citizen", "the states",
    "citizen of", "Latin American", "North American",
    # Decide-b round 4: pending across a separator, ability and plan wording, labelled lines, typos,
    # demonyms, bracketed statuses, question echoes, whitespace runs.
    "lottery", "H-1B lottery", "in process", "(pending)", "- pending", "TN-eligible", "can get a", "could get",
    "plan to get", "planning to apply for", "relocate to the US on a", "relocating to the US with", "move to",
    "Canada: citizen", "US: needs sponsorship", "Canada: PR", "USA: sponsorship required", ": open work permit",
    ": student", "citizan", "Canadain", "authroized", "Autorized", "I'm", "im", "living", "who", "Need an LMIA",
    "LMIA", "would need a new LMIA", "Need H-1B sponsorship", "will need H-1B", "Asylum seeker", "with open work permit",
    "Refugee status in", "protected person", "post grad work permit", "postgraduate work permit", "None",
    "None needed", "citizen of both", "work in both", "(citizen)", "yes (citizen)", "not yet authorized",
    "don't currently have", "Do you require sponsorship?", "Are you authorized?", "No, I need sponsorship",
    "No - I will require sponsorship", "nope", "yep", "nah", "N/A - citizen", "I-485", "applying for PR",
    "eligible to get", "seeking", "planning", " " * 40, "\t\t", "\n\n", "\r\n", NBSP * 3, "Canada and Canada and",
]

EXOTIC_WORDS = [
    f"y{E_ACUTE}s", f"n{E_ACUTE}", f"citizen{E_ACUTE}", f"{C_CEDILLA}itizen", f"caf{E_ACUTE}", f"Canadian{A_GRAVE}",
    f"{E_ACUTE}US", f"n{O_UMLAUT}", f"{DOTTED_CAPITAL_I}stanbul", DOTLESS_I, f"{LONG_S}eattle", KELVIN_SIGN,
    f"stra{SHARP_S}e", FULLWIDTH_DIGITS, ARABIC_INDIC_DIGITS, f"Z{U_UMLAUT}rich", f"Montr{E_ACUTE}al",
    f"Qu{E_ACUTE}bec", f"S{A_TILDE}o",
]

SEPARATORS = [" "] * 12 + [
    ", ", ",", ";", " - ", "/", NBSP, "\t", "\n", "  ", ". ", LINE_SEPARATOR, BOM, EM_SPACE, IDEOGRAPHIC_SPACE,
    NEL, UNIT_SEPARATOR, ZERO_WIDTH_SPACE, NBSP + NBSP,
]
#: What can pad a separator in an address: whitespace of every kind, including the kinds the two languages
#: disagree about (the part splitter must treat all of it as the JavaScript `\s*[,;\n]\s*` did).
PADDING = [" ", "\t", "\n", "\v", "\f", "\r", NBSP, BOM, NEL, EM_SPACE, IDEOGRAPHIC_SPACE, LINE_SEPARATOR,
           ZERO_WIDTH_SPACE, UNIT_SEPARATOR]

STREET_NUMBERS = ["1055", "77", "12345", "4B", "10", "2", "221B", "".join(chr(0xFF10 + d) for d in (2, 3)),
                  "12-14", "4520", "3e", f"1{E_GRAVE}re"]
STREET_NAMES = ["W Georgia St", "Massachusetts Ave", "Main St", "King St W", "rue Saint-Denis", f"Torstra{SHARP_S}e",
                "Bermondsey Street", "Elm Rd", "Example Street", "O'Connor Dr", "avenue du Parc"]
UNITS = ["Apt 4B", "Unit 5", "#12", "Suite 200", "app. 3", "Ste 100", "apartment 9", "apt. 2", "# 7", "Unit"]
CITIES = ["Toronto", "Vancouver", f"Montr{E_ACUTE}al", "Montreal", "Seattle", "San Jose", "Cambridge", "Springfield",
          "London", "Berlin", "Mumbai", "New York", "Washington", "Remote", "Greater Toronto Area", "Hybrid",
          "Quebec City", "St. John's", "O'Fallon", "Winston-Salem", f"Z{U_UMLAUT}rich", "Ottawa", "Waterloo", "Boston",
          "Austin", "Paris", "Anywhere", "Bay Area", "Region of Peel", "NYC", "San Francisco", "washington dc",
          "Georgia", "Indiana", "Victoria", "Hamilton", f"S{A_TILDE}o Paulo", "123", "A1"]
REGIONS = ["ON", "BC", "QC", "Ontario", "Quebec", f"Qu{E_ACUTE}bec", "MA", "WA", "CA", "California", "NY", "New York",
           "DC", "Washington DC", "D.C.", "NL", "PE", "pei", "TX", "Texas", "on", "bc", "AB", "Alberta", "PR",
           "Puerto Rico", "ZZ", f"Nouvelle-{E_ACUTE_CAPITAL}cosse", "washington d.c."]
COUNTRY_NAMES = ["Canada", "CA", "US", "USA", "U.S.", "United States", "United States of America", "United Kingdom",
                 "UK", "Germany", "India", "Canadian", "Mexico", "Deutschland", "france", "the Netherlands", "IN",
                 "DE", "GB", "Great Britain", "Narnia", "America", "states"]
POSTAL_CODES = ["V6E 3P3", "V6E3P3", "v6e 3p3", "M5V 2T6", "K1A 0B1", "02139", "62701", "62701-1234", "SE1 3TQ",
                "sw1a 1aa", "10119", "H2J 2L3", f"M5V{NBSP}2T6", "M5V-2T6", "D1A 0B1", "12345", "9021", "K1A  0B1"]
SEPARATORS_IN_ADDRESS = [", "] * 8 + [",", " , ", ";", "\n", " ", ",,", "; ", ",\n", NBSP + "," + NBSP]
YES_NO = ["Yes", "No", "", "", "", " ", "yes ", "n", "N/A", "NA", "n / a", "Not applicable", "Not yet",
          "Not currently, but I will in the future", "Not required now but will be after OPT", "TBD", "Y", "maybe", "None", "Not sure", "TRUE", "false", "Oui",
          "Non", "yes.", NBSP + "Yes", BOM + "No", "nope", "Yep", "Do you require sponsorship? No I do not",
          "Do you now or in the future require sponsorship? No.", "N/A - citizen", "N/A (US citizen)",
          "Will you need sponsorship?", "No" + " " * 30 + "until 2027", "Not" + "\t\n " + "yet"]
COUNTRY_QUERIES = [None, "", "US", "CA", "GB", "DE", "AT", "FR", "CH", "IN", "MX", "NO", "ES", "IE"]
RESIDENCE_QUERIES = [None, "US", "CA", "DE", "GB", "IN"]


class _Generator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def _case(self, word: str) -> str:
        roll = self.rng.random()
        if roll < 0.18:
            return word.upper()
        if roll < 0.30:
            return word.lower()
        if roll < 0.36:
            return word.capitalize()
        return word

    def text(self, max_words: int = 9) -> str:
        rng = self.rng
        count = rng.randint(0, max_words)
        parts: list[str] = []
        for i in range(count):
            parts.append(rng.choice(EXOTIC_WORDS) if rng.random() < 0.06 else self._case(rng.choice(WORDS)))
            if i < count - 1:
                parts.append(rng.choice(SEPARATORS))
        out = "".join(parts)
        if rng.random() < 0.05:
            out = rng.choice(SEPARATORS) + out + rng.choice(SEPARATORS)
        return out

    def _padding(self) -> str:
        return "".join(self.rng.choice(PADDING) for _ in range(self.rng.randint(0, 6)))

    def address(self) -> str:
        rng = self.rng
        parts: list[str] = []
        roll = rng.random()
        if roll < 0.55:
            parts.append(rng.choice(STREET_NUMBERS) + " " + rng.choice(STREET_NAMES))
            if rng.random() < 0.3:
                parts.append(rng.choice(UNITS))
        elif roll < 0.62:
            parts.append(rng.choice(UNITS))
        if rng.random() < 0.8:
            parts.append(rng.choice(CITIES))
        if rng.random() < 0.65:
            region = rng.choice(REGIONS)
            if rng.random() < 0.4:
                region += " " + rng.choice(POSTAL_CODES)
            parts.append(region)
        elif rng.random() < 0.4:
            parts.append(rng.choice(POSTAL_CODES))
        if rng.random() < 0.45:
            parts.append(rng.choice(COUNTRY_NAMES))
        if rng.random() < 0.15:
            rng.shuffle(parts)
        out = ""
        for i, part in enumerate(parts):
            out += part
            if i < len(parts) - 1:
                if rng.random() < 0.2:  # a separator padded with whitespace of every kind
                    out += self._padding() + rng.choice([",", ";", "\n"]) + self._padding()
                else:
                    out += rng.choice(SEPARATORS_IN_ADDRESS)
        if rng.random() < 0.06:
            out = rng.choice(SEPARATORS) + out + rng.choice(SEPARATORS)
        if rng.random() < 0.03:
            out = rng.choice(["United States", "Remote", "", "  ", "Toronto", ",,;;", "New York", "Canada",
                              "Remote, Canada"])
        return out

    def profile(self) -> dict:
        rng = self.rng
        profile: dict[str, Optional[str]] = {}

        def put(key: str, value: str, absent: float = 0.05) -> None:
            if rng.random() < absent:
                return
            profile[key] = None if rng.random() < 0.03 else value

        put("location", self.address() if rng.random() < 0.7
            else rng.choice(["", "United States", "Remote", "Toronto", "Seattle, WA"]))
        put("addressStreet", self.address() if rng.random() < 0.4 else rng.choice(["", "", "123 Example Street", "  "]))
        put("addressCity", rng.choice(CITIES + ["", "", ""]) if rng.random() < 0.6 else "")
        put("addressState", rng.choice(REGIONS + ["", ""]) if rng.random() < 0.5 else "")
        put("postalCode", rng.choice(POSTAL_CODES + ["", ""]) if rng.random() < 0.5 else "")
        put("country", rng.choice(COUNTRY_NAMES + ["", "", " CA ", NBSP + "US"]) if rng.random() < 0.5 else "")
        put("workAuthorization", self.text() if rng.random() < 0.9 else "")
        put("requiresSponsorship", rng.choice(YES_NO) if rng.random() < 0.7 else self.text(5))
        put("authorizedUS", rng.choice(YES_NO))
        put("authorizedCanada", rng.choice(YES_NO))
        return profile

    def queries(self) -> list[list[Optional[str]]]:
        rng = self.rng
        queries: list[list[Optional[str]]] = [["US", "CA"], ["CA", "CA"], [None, "CA"], ["US", None], [None, None],
                                              ["US", "US"], ["CA", "US"]]
        for _ in range(6):
            queries.append([rng.choice(COUNTRY_QUERIES), rng.choice(RESIDENCE_QUERIES)])
        return queries


def generate_corpus(seed: int, per_kind: int) -> list[dict]:
    """``per_kind`` cases of each of the five kinds, the same corpus for the same seed."""
    gen = _Generator(seed)
    rng = gen.rng
    cases: list[dict] = []
    for _ in range(per_kind):
        cases.append({"kind": "polarity", "text": gen.text(6) if rng.random() < 0.8 else rng.choice(YES_NO)})
    for _ in range(per_kind):
        cases.append({"kind": "countriesIn", "text": gen.text()})
    for _ in range(per_kind):
        cases.append({"kind": "parseAddress", "text": gen.address()})
    for _ in range(per_kind):
        cases.append({"kind": "location", "profile": gen.profile()})
    for _ in range(per_kind):
        cases.append({"kind": "workAuth", "profile": gen.profile(), "queries": gen.queries()})
    return cases


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare backend.services.profile_facts with the real TypeScript.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cases", type=int, default=DEFAULT_PER_KIND, help="cases of each of the five kinds")
    parser.add_argument("--tables", action="store_true", help="only compare the geo tables")
    parser.add_argument("--show-sources", action="store_true", help="list the rules the corpus reached")
    args = parser.parse_args(argv)

    reason = unavailable_reason()
    if reason is not None:
        print(reason, file=sys.stderr)
        return 2
    problems = table_mismatches()
    print("geo tables:", "identical" if not problems else "DIFFER")
    for problem in problems:
        print("  ", problem)
    if args.tables:
        return 1 if problems else 0

    cases = generate_corpus(args.seed, args.cases)
    reference = run_reference(cases)
    mismatches = compare(cases, reference)
    reached = rule_sources_reached(cases, reference)
    print(f"seed {args.seed}: {len(cases)} cases ({args.cases} of each kind), {len(mismatches)} mismatches, "
          f"{len(reached)} distinct rules reached")
    if args.show_sources:
        for name in sorted(reached):
            print("  ", name)
    missed = EXPECTED_RULE_SOURCES - reached
    if missed:
        print("rules never reached by this corpus:", sorted(missed))
    for message in mismatches[:10]:
        print(message)
    return 1 if (mismatches or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
