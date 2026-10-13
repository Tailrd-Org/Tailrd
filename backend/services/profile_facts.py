"""
Profile facts: the candidate's work authorisation and location, read from their saved
answers exactly the way the browser extension reads them.

This is a PORT, not a redesign. The source of truth is
``chrome-extension/src/content/profileFacts.ts`` and the geo tables in ``geo.ts`` that it
leans on. The extension answers application forms from these facts; the match engine has to
read a saved profile the same way, or a job the extension would call "authorised" gets scored
as "needs sponsorship" here (and the reverse). So every rule, edge case, confidence and
``source`` string below is copied, including the ones that look wrong. Those are listed under
"Behaviour kept on purpose" and pinned by ``backend/tests/test_profile_facts.py``. Fixing one
is a decision for both sides at once, never for this file alone.

What it reads
    Ten text fields of the application profile, under the camelCase names the extension
    receives: ``location``, ``addressStreet``, ``addressCity``, ``addressState``,
    ``postalCode``, ``country``, ``workAuthorization``, ``requiresSponsorship``,
    ``authorizedUS``, ``authorizedCanada``. Pass the ``ApplicationProfileOut`` that
    ``build_application_profile`` returns, any object with those attributes (the /api/fill
    ``ApplicantProfile``), or the dict the extension gets as JSON. An absent, null or
    non-string field reads as blank. Blank means "not answered", never "no".

What it says
    Every fact carries a confidence, which is a promise about what may happen next:
    ``high`` the profile settles it; ``medium`` likely, but a real person could differ (a bare
    "Toronto" is probably Canada, and there is a London, Ontario); ``low`` a hint.
    ``unknown`` is ``None``, never a guess.

    * An unscoped statement ("Yes", "Canadian citizen" with no country to attach it to) is
      about the country the applicant lives in. Their residence is ``residence_of(...)``:
      the country of ``location_facts``, and only when it is high confidence.
    * An explicit per-country answer ("Authorized to work in the US: No") wins over the
      free-text reading for that country.

Pure: no I/O, no database, no clock, no network. And linear: profile text has no length limit
upstream, so every function costs time proportional to the length of its input (pinned by the
hostile-input tests in test_profile_facts.py).

JavaScript regular expressions
    The patterns are the TypeScript's, copied as written. Two are re-expressed because Python
    cannot say them: the Unicode-letter test of a city (``_is_plain_words``) and the
    variable-width lookbehind before "America" (four fixed-width ones in ``_NAME_US``).
    JavaScript and Python also disagree about three things in all the others, so
    ``_js_regex`` compiles every pattern the way JavaScript reads it: ``\\b``, ``\\d`` and
    case-insensitivity are ASCII-only (Python's default is Unicode) and ``\\s`` is
    JavaScript's whitespace set (which pasted text hits: a non-breaking space is whitespace,
    U+0085 is not). ``_trim`` is JavaScript's ``String.prototype.trim`` for the same reason.

Behaviour kept on purpose (each one is pinned by a test named ``test_pin_*``)
    * An unrecognised nationality ("Iranian citizen", "French citizen": the table has no
      such demonym) names no country, so it reads as an unscoped yes: authorised in
      whatever country the applicant lives in.
    * One reading is applied to every country a sentence names, so "Authorized to work in
      Canada but not in the US" is authorised in the US, and "...Canada, not authorized in
      the US" is not authorised in Canada.
    * "N/A", "Not sure" and "Not applicable" start with the word "n/not" and read as No.
    * A location of just "United States" (the UserSettings.location column default) is a
      settled residence in the US, and it outranks a structured province.
    * An explicit Yes over a free-text denial keeps the basis ``denied``.
    * A state that is also a city ("New York") is consumed as the region; no city is left.
    * A five-digit postcode is a US ZIP, wherever it is.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Generic, Literal, Optional, Protocol, TypeVar, Union

__all__ = [
    "CA_PROVINCES",
    "COUNTRIES",
    "FREE_MOVEMENT",
    "US_STATES",
    "AuthBasis",
    "Confidence",
    "Country",
    "CountryAuth",
    "Fact",
    "LocationFacts",
    "ParsedAddress",
    "PostalCode",
    "ProfileFields",
    "ProfileInput",
    "Region",
    "WorkAuthFacts",
    "authorized_in",
    "countries_in",
    "country_of",
    "location_facts",
    "needs_sponsorship_in",
    "negated_country_scope",
    "parse_address",
    "polarity_of",
    "residence_of",
    "work_auth_facts",
]

T = TypeVar("T")

Confidence = Literal["high", "medium", "low"]
AuthBasis = Literal["citizen", "permanent_resident", "work_permit", "student", "statement", "denied", "stated"]


class ProfileFields(Protocol):
    """The text fields the facts read, under the names the profile schema gives them (camelCase).

    An ``ApplicationProfileOut`` (what ``build_application_profile`` returns) and the /api/fill
    ``ApplicantProfile`` have all ten. A test pins this list to the fields the code actually reads
    and to both schemas, so a renamed schema field cannot silently read as blank.
    """

    location: str
    addressStreet: str
    addressCity: str
    addressState: str
    postalCode: str
    country: str
    workAuthorization: str
    requiresSponsorship: str
    authorizedUS: str
    authorizedCanada: str


#: What every ``profile`` argument accepts: the dict the extension receives as JSON (the fields above
#: as keys), or an object that has them as attributes. Both are read through ``_text``.
ProfileInput = Union[Mapping[str, Any], ProfileFields]


# ---------------------------------------------------------------------------
# Types (mirror the TypeScript ones; frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact(Generic[T]):
    """A derived value, the promise made about it, and which rule produced it."""

    value: T
    confidence: Confidence
    #: Which rule produced it, for telemetry and audit trails ("work-auth:citizen").
    source: str


@dataclass(frozen=True)
class Country:
    code: str  # ISO 3166-1 alpha-2
    name: str  # the name most forms list
    continent: str
    aliases: tuple[str, ...]  # other spellings, abbreviations, demonyms (lowercase, no punctuation)


@dataclass(frozen=True)
class Region:
    """A US state or Canadian province."""

    code: str
    name: str
    country: Literal["US", "CA"]
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostalCode:
    code: str
    country: str  # the country whose postcode shape it has


@dataclass(frozen=True)
class ParsedAddress:
    """The parts of one free-text address that could be identified; the rest are None."""

    street: Optional[str] = None
    city: Optional[str] = None
    region: Optional[Region] = None
    postal: Optional[PostalCode] = None
    country: Optional[Country] = None


@dataclass(frozen=True)
class LocationFacts:
    street: Optional[Fact[str]]
    city: Optional[Fact[str]]
    region: Optional[Fact[Region]]
    postal_code: Optional[Fact[str]]
    country: Optional[Fact[Country]]


@dataclass(frozen=True)
class CountryAuth:
    #: Authorized to work there: True / False, or None when the profile does not say.
    authorized: Optional[bool]
    #: Needs sponsorship now or in the future there.
    needs_sponsorship: Optional[bool]
    basis: AuthBasis


class _ReadOnlyDict(dict):
    """A dict that refuses every change after construction.

    Still a ``dict`` (equality, JSON, ``dataclasses.asdict``), and it pickles, deep-copies and hashes
    as itself. The same pattern as ``ReadOnlyDict`` in match-engine/src/matcher/score/prepare.py,
    copied rather than imported (this package does not depend on match-engine); the ``__hash__`` is
    this copy's addition, so that the frozen facts holding one are hashable. ``__reduce__`` matters:
    pickle and copy rebuild a dict subclass with ``obj[key] = value``, which this class refuses, so
    they must rebuild it through the constructor instead.
    """

    __slots__ = ()

    def _refuse(self, *args: object, **kwargs: object) -> None:
        raise TypeError(f"{type(self).__name__} is read-only")

    __setitem__ = __delitem__ = __ior__ = _refuse  # type: ignore[assignment]
    clear = pop = popitem = setdefault = update = _refuse  # type: ignore[assignment]

    def __reduce__(self) -> tuple[type, tuple[dict]]:
        return (type(self), (dict(self),))

    def __hash__(self) -> int:  # equal dicts hash alike: the order of the items does not matter
        return hash(frozenset(self.items()))


@dataclass(frozen=True)
class WorkAuthFacts:
    #: Per ISO country code, in the order they were found. Only countries the profile speaks about.
    #: A read-only dict (``_ReadOnlyDict``); typed as the read-only ``Mapping`` it is used as.
    by_country: Mapping[str, CountryAuth]
    #: The applicant's own yes/no on "do you require sponsorship", if stated.
    stated_sponsorship: Optional[bool]
    #: A statement that named NO country ("Yes", "Authorized to work").
    unscoped_authorized: Optional[bool]


# ---------------------------------------------------------------------------
# JavaScript-compatible text handling
# ---------------------------------------------------------------------------

# JavaScript's whitespace: what String.prototype.trim() strips and what \s matches. Python's
# str.strip() and \s differ at the edges (U+FEFF is JavaScript whitespace and not Python's;
# U+0085 and U+001C-U+001F are Python's and not JavaScript's), so the exact set is spelled out.
_JS_SPACE = (
    "\t\n\v\f\r \u00a0\u1680"
    + "".join(chr(c) for c in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _trim(text: str) -> str:
    return text.strip(_JS_SPACE)


def _js_regex(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """Compile ``pattern`` the way JavaScript reads it (see the module docstring).

    ``re.ASCII`` makes ``\\b``, ``\\w``, ``\\d`` and IGNORECASE ASCII-only, as in JavaScript.
    It also makes ``\\s`` ASCII-only, which JavaScript's is not, so ``\\s`` / ``\\S`` are
    rewritten to the JavaScript whitespace set first (a class body when already inside one).
    """
    out: list[str] = []
    in_class = False
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt == "s":
                out.append(_JS_SPACE if in_class else f"[{_JS_SPACE}]")
            elif nxt == "S":
                if in_class:
                    raise ValueError("\\S inside a character class is not supported")
                out.append(f"[^{_JS_SPACE}]")
            else:
                out.append(ch + nxt)
            i += 2
            continue
        if ch == "[" and not in_class:
            in_class = True
        elif ch == "]" and in_class:
            in_class = False
        out.append(ch)
        i += 1
    return re.compile("".join(out), flags | re.ASCII)


def _text(profile: ProfileInput, name: str) -> str:
    """One text field of a profile; absent, null and non-string values read as blank.

    The single place that knows a profile is either a mapping (the extension's JSON) or an
    object (ApplicationProfileOut, ApplicantProfile). The field names are the schema's own.
    """
    value = profile.get(name) if isinstance(profile, Mapping) else getattr(profile, name, None)
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Geo reference data (geo.ts): countries, regions, postcodes, well-known cities
# ---------------------------------------------------------------------------
# Deliberately small and exact: a place the tables do not know simply yields no fact.

COUNTRIES: tuple[Country, ...] = (
    Country("US", "United States", "North America", (
        "us", "usa", "u s", "u s a", "united states of america", "america", "american",
        "the united states", "the us", "the usa", "states",
    )),
    Country("CA", "Canada", "North America", ("can", "canadian")),
    Country("MX", "Mexico", "North America", ("mex", "mexican", "mexico")),
    Country("GB", "United Kingdom", "Europe", (
        "gbr", "uk", "u k", "great britain", "britain", "british", "england", "scotland", "wales",
        "northern ireland", "the uk", "the united kingdom",
    )),
    Country("IE", "Ireland", "Europe", ("irl", "irish", "republic of ireland")),
    Country("FR", "France", "Europe", ("fra", "french republic")),
    Country("DE", "Germany", "Europe", ("deu", "german", "deutschland")),
    Country("NL", "Netherlands", "Europe", ("nld", "the netherlands", "holland", "dutch")),
    Country("BE", "Belgium", "Europe", ("bel", "belgian")),
    Country("ES", "Spain", "Europe", ("esp", "spanish", "espana")),
    Country("PT", "Portugal", "Europe", ("prt", "portuguese")),
    Country("IT", "Italy", "Europe", ("ita", "italian", "italia")),
    Country("CH", "Switzerland", "Europe", ("che", "swiss")),
    Country("AT", "Austria", "Europe", ("aut", "austrian")),
    Country("SE", "Sweden", "Europe", ("swe", "swedish")),
    Country("NO", "Norway", "Europe", ("nor", "norwegian")),
    Country("DK", "Denmark", "Europe", ("dnk", "danish")),
    Country("FI", "Finland", "Europe", ("fin", "finnish")),
    Country("PL", "Poland", "Europe", ("pol", "polish")),
    Country("CZ", "Czech Republic", "Europe", ("czechia", "czech")),
    Country("RO", "Romania", "Europe", ("romanian",)),
    Country("GR", "Greece", "Europe", ("greek",)),
    Country("UA", "Ukraine", "Europe", ("ukrainian",)),
    Country("TR", "Turkey", "Asia", ("turkiye", "turkish")),
    Country("IL", "Israel", "Asia", ("isr", "israeli")),
    Country("AE", "United Arab Emirates", "Asia", ("uae", "u a e", "emirates")),
    Country("SA", "Saudi Arabia", "Asia", ("saudi", "ksa")),
    Country("IN", "India", "Asia", ("ind", "indian", "bharat")),
    Country("PK", "Pakistan", "Asia", ("pak", "pakistani")),
    Country("BD", "Bangladesh", "Asia", ("bangladeshi",)),
    Country("LK", "Sri Lanka", "Asia", ("sri lankan",)),
    Country("CN", "China", "Asia", ("chn", "chinese", "prc", "people s republic of china", "mainland china")),
    Country("HK", "Hong Kong", "Asia", ("hkg",)),
    Country("TW", "Taiwan", "Asia", ("taiwanese",)),
    Country("JP", "Japan", "Asia", ("jpn", "japanese")),
    Country("KR", "South Korea", "Asia", ("kor", "korea", "republic of korea", "korean")),
    Country("SG", "Singapore", "Asia", ("sgp", "singaporean")),
    Country("MY", "Malaysia", "Asia", ("malaysian",)),
    Country("PH", "Philippines", "Asia", ("phl", "the philippines", "filipino")),
    Country("VN", "Vietnam", "Asia", ("viet nam", "vietnamese")),
    Country("TH", "Thailand", "Asia", ("thai",)),
    Country("ID", "Indonesia", "Asia", ("indonesian",)),
    Country("AU", "Australia", "Oceania", ("aus", "australian")),
    Country("NZ", "New Zealand", "Oceania", ("nzl", "kiwi")),
    Country("BR", "Brazil", "South America", ("bra", "brasil", "brazilian")),
    Country("AR", "Argentina", "South America", ("arg", "argentinian", "argentine")),
    Country("CL", "Chile", "South America", ("chilean",)),
    Country("CO", "Colombia", "South America", ("colombian",)),
    Country("PE", "Peru", "South America", ("peruvian",)),
    Country("NG", "Nigeria", "Africa", ("nga", "nigerian")),
    Country("KE", "Kenya", "Africa", ("kenyan",)),
    Country("ZA", "South Africa", "Africa", ("zaf", "south african")),
    Country("EG", "Egypt", "Africa", ("egyptian",)),
    Country("MA", "Morocco", "Africa", ("moroccan",)),
    Country("GH", "Ghana", "Africa", ("ghanaian",)),
)

# CA_PROVINCES and US_STATES intentionally duplicate location_parser.CA_PROVINCES / US_STATES. They
# are the extension's geo.ts tables, which carry what location_parser's code-to-name dicts do not:
# Puerto Rico, the aliases ("pei", "pq", "washington dc", the French province names), and parity
# with the TypeScript depends on every row. Do not dedupe them into location_parser's.
CA_PROVINCES: tuple[Region, ...] = (
    Region("AB", "Alberta", "CA"),
    Region("BC", "British Columbia", "CA", ("colombie britannique",)),
    Region("MB", "Manitoba", "CA"),
    Region("NB", "New Brunswick", "CA", ("nouveau brunswick",)),
    Region("NL", "Newfoundland and Labrador", "CA", ("newfoundland", "terre neuve et labrador")),
    Region("NS", "Nova Scotia", "CA", ("nouvelle ecosse",)),
    Region("NT", "Northwest Territories", "CA", ("territoires du nord ouest",)),
    Region("NU", "Nunavut", "CA"),
    Region("ON", "Ontario", "CA"),
    Region("PE", "Prince Edward Island", "CA", ("pei", "ile du prince edouard")),
    Region("QC", "Quebec", "CA", ("pq", "province de quebec")),
    Region("SK", "Saskatchewan", "CA"),
    Region("YT", "Yukon", "CA", ("yukon territory",)),
)

# (See the note above CA_PROVINCES: a deliberate duplicate of location_parser's table, for TS parity.)
US_STATES: tuple[Region, ...] = (
    Region("AL", "Alabama", "US"),
    Region("AK", "Alaska", "US"),
    Region("AZ", "Arizona", "US"),
    Region("AR", "Arkansas", "US"),
    Region("CA", "California", "US"),
    Region("CO", "Colorado", "US"),
    Region("CT", "Connecticut", "US"),
    Region("DE", "Delaware", "US"),
    Region("FL", "Florida", "US"),
    Region("GA", "Georgia", "US"),
    Region("HI", "Hawaii", "US"),
    Region("ID", "Idaho", "US"),
    Region("IL", "Illinois", "US"),
    Region("IN", "Indiana", "US"),
    Region("IA", "Iowa", "US"),
    Region("KS", "Kansas", "US"),
    Region("KY", "Kentucky", "US"),
    Region("LA", "Louisiana", "US"),
    Region("ME", "Maine", "US"),
    Region("MD", "Maryland", "US"),
    Region("MA", "Massachusetts", "US"),
    Region("MI", "Michigan", "US"),
    Region("MN", "Minnesota", "US"),
    Region("MS", "Mississippi", "US"),
    Region("MO", "Missouri", "US"),
    Region("MT", "Montana", "US"),
    Region("NE", "Nebraska", "US"),
    Region("NV", "Nevada", "US"),
    Region("NH", "New Hampshire", "US"),
    Region("NJ", "New Jersey", "US"),
    Region("NM", "New Mexico", "US"),
    Region("NY", "New York", "US"),
    Region("NC", "North Carolina", "US"),
    Region("ND", "North Dakota", "US"),
    Region("OH", "Ohio", "US"),
    Region("OK", "Oklahoma", "US"),
    Region("OR", "Oregon", "US"),
    Region("PA", "Pennsylvania", "US"),
    Region("RI", "Rhode Island", "US"),
    Region("SC", "South Carolina", "US"),
    Region("SD", "South Dakota", "US"),
    Region("TN", "Tennessee", "US"),
    Region("TX", "Texas", "US"),
    Region("UT", "Utah", "US"),
    Region("VT", "Vermont", "US"),
    Region("VA", "Virginia", "US"),
    Region("WA", "Washington", "US"),
    Region("WV", "West Virginia", "US"),
    Region("WI", "Wisconsin", "US"),
    Region("WY", "Wyoming", "US"),
    Region("DC", "District of Columbia", "US", ("washington dc", "washington d c")),
    Region("PR", "Puerto Rico", "US"),
)

# Cities common enough in our users' profiles that a bare "Toronto" is evidence of its
# country. Evidence, not proof (there is a London, Ontario): facts derived from this table
# are capped at MEDIUM confidence.
_CITY_COUNTRY: Mapping[str, str] = MappingProxyType({
    "toronto": "CA", "montreal": "CA", "vancouver": "CA", "ottawa": "CA", "calgary": "CA", "edmonton": "CA",
    "winnipeg": "CA", "mississauga": "CA", "brampton": "CA", "hamilton": "CA", "kitchener": "CA",
    "waterloo": "CA", "markham": "CA", "quebec city": "CA", "halifax": "CA", "victoria": "CA",
    "saskatoon": "CA", "regina": "CA", "burnaby": "CA", "surrey": "CA", "laval": "CA", "gatineau": "CA",
    "oakville": "CA", "guelph": "CA", "new york": "US", "new york city": "US", "nyc": "US",
    "san francisco": "US", "seattle": "US", "boston": "US", "chicago": "US", "austin": "US",
    "los angeles": "US", "san jose": "US", "palo alto": "US", "denver": "US", "atlanta": "US",
    "miami": "US", "dallas": "US", "houston": "US", "washington dc": "US", "philadelphia": "US",
})

# The EU, the rest of the EEA and Switzerland: a citizen of one may work in any other
# (freedom of movement).
FREE_MOVEMENT: frozenset[str] = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
        "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "CH",
    }
)

_COMBINING_MARKS = re.compile("[\u0300-\u036f]")
_NOT_ALNUM = re.compile("[^a-z0-9]+")


def _geo_norm(text: Optional[str]) -> str:
    """Lowercase, accents stripped, punctuation to space (geoNorm)."""
    decomposed = unicodedata.normalize("NFD", text or "")
    return _NOT_ALNUM.sub(" ", _COMBINING_MARKS.sub("", decomposed).lower()).strip()


def _index_countries(countries: tuple[Country, ...]) -> Mapping[str, Country]:
    """Every spelling a country answers to (name, code, aliases), as a read-only mapping.

    A later country wins a clash, as in the extension's table.
    """
    index: dict[str, Country] = {}
    for country in countries:
        index[_geo_norm(country.name)] = country
        index[country.code.lower()] = country
        for alias in country.aliases:
            index[_geo_norm(alias)] = country
    return MappingProxyType(index)


def _index_regions(regions: tuple[Region, ...]) -> Mapping[str, tuple[Region, ...]]:
    """Every spelling a state or province answers to, with the regions that answer to it (table order)."""
    found: dict[str, list[Region]] = {}
    for region in regions:
        for key in {region.code.lower(), _geo_norm(region.name), *(_geo_norm(a) for a in region.aliases)}:
            found.setdefault(key, []).append(region)
    return MappingProxyType({key: tuple(hits) for key, hits in found.items()})


_COUNTRY_BY_KEY = _index_countries(COUNTRIES)
_COUNTRY_BY_CODE: Mapping[str, Country] = MappingProxyType({c.code: c for c in COUNTRIES})
_REGIONS_BY_KEY = _index_regions((*CA_PROVINCES, *US_STATES))  # provinces first, then states


def _country_from_name(text: Optional[str]) -> Optional[Country]:
    """A country named by ``text`` as a WHOLE ("Canada", "USA", "U.S.", "Canadian").

    Two-letter ISO codes other than US/UK are NOT accepted: "CA", "IN", "DE" are far more
    often a US state in a location string than a country code.
    """
    key = _geo_norm(text)
    if not key:
        return None
    if len(key) == 2 and key not in ("us", "uk"):
        return None
    return _COUNTRY_BY_KEY.get(key)


def _country_by_code(code: str) -> Optional[Country]:
    return _COUNTRY_BY_CODE.get(code.upper())


def _region_from_text(text: Optional[str], prefer: Optional[str] = None) -> Optional[Region]:
    """A US state / Canadian province named or coded by ``text`` as a whole."""
    key = _geo_norm(text)
    if not key:
        return None
    hits = _REGIONS_BY_KEY.get(key, ())
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    return next((h for h in hits if h.country == prefer), None)


# Postal / ZIP code shapes, each pinning the country it belongs to.
_CA_POSTAL = _js_regex(r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z])\s?(\d[ABCEGHJ-NPRSTV-Z]\d)\b", re.IGNORECASE)
_US_ZIP = _js_regex(r"\b(\d{5})(?:-(\d{4}))?\b")
_UK_POSTCODE = _js_regex(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s(\d[A-Z]{2})\b", re.IGNORECASE)


def _find_postal_code(text: str) -> Optional[PostalCode]:
    ca = _CA_POSTAL.search(text)
    if ca:
        return PostalCode(f"{ca[1]} {ca[2]}".upper(), "CA")
    uk = _UK_POSTCODE.search(text)
    if uk:
        return PostalCode(f"{uk[1]} {uk[2]}".upper(), "GB")
    # The LAST five-digit run: a US address leads with its street number
    # ("12345 Main St, Springfield, IL 62701"), the ZIP always comes last.
    zips = list(_US_ZIP.finditer(text))
    if zips:
        us = zips[-1]
        return PostalCode(f"{us[1]}-{us[2]}" if us[2] else us[1], "US")
    return None


def _country_hint_for_city(city: str) -> Optional[str]:
    return _CITY_COUNTRY.get(_geo_norm(city))


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

# Parts are split on the bare separator and each part is trimmed afterwards. That reads every input
# the way the extension's /\s*[,;\n]\s*/ does (the whitespace padding a separator is trimmed off the
# parts it touches) but in one pass: the padded pattern is quadratic on a long whitespace run, and
# profile text has no length limit upstream (10,000 spaces cost 0.4 s, 40,000 cost 6 s).
_PART_SEPARATOR = _js_regex(r"[,;\n]")


def _split_parts(raw: str) -> list[str]:
    """The non-blank parts of an address: split on , ; and newline, each trimmed."""
    return [part for part in (_trim(piece) for piece in _PART_SEPARATOR.split(raw)) if part]


_SPACE_OR_HYPHEN = _js_regex(r"[\s-]")
_STREET_NUMBER = _js_regex(r"^\d+[A-Za-z]?\b\s+\S")
_STREET_WORD = _js_regex(r"^(unit|apt|suite)\b", re.IGNORECASE)
_UNIT_PART = _js_regex(r"^(apt|apartment|unit|suite|ste|#)\b", re.IGNORECASE)
_NOT_A_PLACE = _js_regex(r"\b(remote|anywhere|hybrid|area|region)\b", re.IGNORECASE)


@lru_cache(maxsize=64)
def _postal_pattern(code: str) -> re.Pattern[str]:
    """The postal code as it may be spelled in a part: "V6E 3P3" also matches "V6E3P3" and "V6E-3P3"."""
    return _js_regex(re.sub(r"[ -]", lambda _m: r"[\s-]?", code), re.IGNORECASE)


def _squash(text: str) -> str:
    return _SPACE_OR_HYPHEN.sub("", text).upper()


def _is_letter(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "L"


def _is_plain_words(part: str) -> bool:
    """/^[\\p{L}][\\p{L}\\s.'-]*$/u: a letter, then letters, whitespace, ".", "'" or "-"."""
    if not part or not _is_letter(part[0]):
        return False
    return all(_is_letter(ch) or ch in _JS_SPACE or ch in ".'-" for ch in part[1:])


def parse_address(text: Optional[str]) -> ParsedAddress:
    """Parse one free-text address / location (TS: ``parseAddress``).

    "1055 W Georgia St, Vancouver, BC V6E 3P3, Canada", "Toronto, ON, Canada",
    "Cambridge, MA 02139", "Ottawa". Returns only the parts it could identify.
    """
    raw = _trim(text or "")
    if not raw:
        return ParsedAddress()
    postal = _find_postal_code(raw)
    parts = _split_parts(raw)

    # Country: the last part naming one.
    country: Optional[Country] = None
    for i in range(len(parts) - 1, -1, -1):
        found = _country_from_name(parts[i])
        if found:
            country = found
            del parts[i]
            break

    # Region: a part that IS a state/province, or one that begins with it and carries the
    # postal code ("BC V6E 3P3", "MA 02139").
    prefer = country.code if country and country.code in ("US", "CA") else None
    region: Optional[Region] = None
    for i in range(len(parts) - 1, -1, -1):
        part = parts[i]
        stripped = _trim(_postal_pattern(postal.code).sub("", part, count=1)) if postal else part
        candidate = _region_from_text(stripped, prefer)
        if candidate and (country is None or country.code == candidate.country):
            region = candidate
            del parts[i]
            break

    # Drop a part that is nothing but the postal code.
    if postal:
        wanted = _squash(postal.code)
        parts = [p for p in parts if _squash(p) != wanted]

    # Street: a part opening with a civic number ("1055 W Georgia St", "77 Massachusetts Ave").
    street: Optional[str] = None
    street_idx = next(
        (i for i, p in enumerate(parts) if _STREET_NUMBER.search(p) or _STREET_WORD.search(p)), -1
    )
    if street_idx >= 0:
        street = parts.pop(street_idx)
        # An apartment/unit part right after the street belongs to it.
        if street_idx < len(parts) and _UNIT_PART.search(parts[street_idx]):
            street += f", {parts.pop(street_idx)}"

    # City: what is left, when a plain-word part remains (the last one).
    words = [p for p in parts if _is_plain_words(p)]
    city = words[-1] if words else None
    return ParsedAddress(street=street, city=city, region=region, postal=postal, country=country)


def country_of(parsed: ParsedAddress) -> Optional[Country]:
    """The country a parsed address is in: named, else the one its region exists in, else its
    postcode's (TS: ``countryOf``)."""
    if parsed.country:
        return parsed.country
    if parsed.region:
        return _country_by_code(parsed.region.country)
    if parsed.postal:
        return _country_by_code(parsed.postal.country)
    return None


def location_facts(profile: ProfileInput) -> LocationFacts:
    """Street, city, region, postal code and country, each with the confidence it earns
    (TS: ``locationFacts``)."""
    address_street = _text(profile, "addressStreet")
    from_street = parse_address(address_street)
    from_location = parse_address(_text(profile, "location"))
    street_has_more = bool(from_street.city or from_street.region or from_street.country or from_street.postal)

    # Street: the structured field, minus any city/region/country it carried.
    street: Optional[Fact[str]] = None
    if _trim(address_street):
        if street_has_more:
            if from_street.street:
                street = Fact(from_street.street, "high", "address:parsed-street")
        else:
            street = Fact(_trim(address_street), "high", "profile:addressStreet")

    # Country: stated > a region/postal code that only exists in one country > a named country
    # in the location string. A bare city is only a hint.
    country: Optional[Fact[Country]] = None
    country_text = _text(profile, "country")
    stated = _country_from_name(country_text)
    if stated is None and len(_trim(country_text)) == 2:
        stated = _country_by_code(_trim(country_text))
    if stated:
        country = Fact(stated, "high", "profile:country")
    stated_region = _region_from_text(
        _text(profile, "addressState"), stated.code if stated and stated.code in ("US", "CA") else None
    )
    if country is None:
        found = country_of(from_street)
        if found is None:
            found = country_of(from_location)
        if found is None and stated_region:
            found = _country_by_code(stated_region.country)
        if found:
            country = Fact(found, "high", "address:country")
    if country is None:
        city_guess = _text(profile, "addressCity") or from_location.city or from_street.city
        hinted = _country_hint_for_city(city_guess) if city_guess else None
        hinted_country = _country_by_code(hinted) if hinted else None
        if hinted_country:
            country = Fact(hinted_country, "medium", "city-hint")

    # Region: stated > parsed (only when consistent with the country).
    def region_ok(r: Optional[Region]) -> bool:
        return r is not None and (country is None or country.value.code == r.country)

    region: Optional[Fact[Region]] = None
    if stated_region is not None and region_ok(stated_region):
        region = Fact(stated_region, "high", "profile:addressState")
    elif from_street.region is not None and region_ok(from_street.region):
        region = Fact(from_street.region, "high", "address:region")
    elif from_location.region is not None and region_ok(from_location.region):
        region = Fact(from_location.region, "high", "location:region")

    # City: stated > the address's city > the location string's city.
    city: Optional[Fact[str]] = None
    stated_city = _trim(_text(profile, "addressCity"))
    if stated_city:
        city = Fact(stated_city, "high", "profile:addressCity")
    elif from_street.city and street_has_more:
        city = Fact(from_street.city, "high", "address:city")
    elif from_location.city:
        # "Toronto, ON, Canada" pins the city; a lone word could be anything ("Remote",
        # "Greater Toronto Area"), so it is only a hint on its own.
        pinned = bool(from_location.region or from_location.country or from_location.postal)
        if not _NOT_A_PLACE.search(from_location.city):
            city = Fact(from_location.city, "high" if pinned else "medium", "location:city")

    postal_code: Optional[Fact[str]] = None
    stated_postal = _trim(_text(profile, "postalCode"))
    if stated_postal:
        postal_code = Fact(stated_postal, "high", "profile:postalCode")
    elif from_street.postal:
        postal_code = Fact(from_street.postal.code, "high", "address:postal")
    elif from_location.postal:
        postal_code = Fact(from_location.postal.code, "high", "location:postal")

    return LocationFacts(street=street, city=city, region=region, postal_code=postal_code, country=country)


def residence_of(location: LocationFacts) -> Optional[str]:
    """The ISO code of the country the applicant lives in, only when the profile settles it.

    The rule the extension uses to fill the ``residence`` argument of ``authorized_in`` /
    ``needs_sponsorship_in`` (questionResolver.ts ``residenceOf``): the country, and only at
    high confidence. A bare "Toronto" (medium) is not a residence.

    Warning: a location that is just the default "United States" yields ``"US"`` here, at high
    confidence, exactly as it does in the extension. ``UserSettings.location`` is created with that
    value and ``build_application_profile`` falls back to it, so it says nothing about where the
    applicant lives. This function keeps the extension's reading on purpose; the match-engine
    integration (Task 9.1) ignores that default when it maps these facts. Do not hide the default
    here, or the port and the extension stop agreeing.
    """
    country = location.country
    return country.value.code if country is not None and country.confidence == "high" else None


# ---------------------------------------------------------------------------
# Work authorization
# ---------------------------------------------------------------------------

_YES_PREFIX = _js_regex(r"^(yes|y|true|oui|si)\b")
_NO_PREFIX = _js_regex(r"^(no|n|false|non|none|not)\b")
_NEGATED_NEED = _js_regex(r"\b(will not|won't|do not|don't|does not|doesn't|no longer|never)\s+(need|require)")
_PLAIN_NEED = _js_regex(r"\b(will|do|does|would)\s+(need|require)\b")
_NOT_REQUIRED = _js_regex(r"\b(not required|not needed)\b")
_REQUIRED = _js_regex(r"\b(required|needed)\b")
# "N/A" and its spellings (TS: ``NOT_APPLICABLE``): unanswered, never a No.
_NOT_APPLICABLE = _js_regex(r"^(n\s*[/.]?\s*a\b\.?|not applicable\b|does not apply\b|doesn'?t apply\b)")


def polarity_of(text: Optional[str]) -> Optional[bool]:
    """Yes/no reading of a short stored answer, or None. Blank is None, never False (TS: ``polarityOf``).

    "N/A", "NA", "N.A.", "not applicable" are None: unanswered, never a No (owner decisions
    2026-10-13).
    """
    t = _trim(text or "").lower()
    if not t:
        return None
    if _NOT_APPLICABLE.search(t):
        return None
    if _YES_PREFIX.search(t):
        return True
    if _NO_PREFIX.search(t):
        return False
    if _NEGATED_NEED.search(t):
        return False
    if _PLAIN_NEED.search(t):
        return True
    if _NOT_REQUIRED.search(t):
        return False
    if _REQUIRED.search(t):
        return True
    return None


# Bare "us" is a pronoun ("work for us") unless it follows "the"/"in"; "America" is the US
# only when no continent word precedes it. (Python's lookbehind must be fixed-width, so the
# JavaScript (?<!(north|south|latin|central) ) is four lookbehinds.)
_NAME_US = _js_regex(
    r" (united states of america|united states|u s a|u s|usa)  ?"
    r"| (the|in) us "
    r"| (?<!north )(?<!south )(?<!latin )(?<!central )(america|american) "
)
_NAME_CA = _js_regex(r" (canada|canadian) ")
_NAME_GB = _js_regex(r" (united kingdom|great britain|britain|british|england|uk|u k) ")
_NAME_PATTERNS = ((_NAME_US, "US"), (_NAME_CA, "CA"), (_NAME_GB, "GB"))
# "US Citizen": "US" in capitals is the country (lowercase "us" stays a pronoun).
_CAPITAL_US = _js_regex(r"(^|[^A-Za-z])U\.?S\.?(?![A-Za-z])")
_US_ONLY_STATUS = _js_regex(r" (green card|h ?1 ?b|h ?4|l ?1|tn|e ?3|o ?1|f ?1|j ?1|m ?1|opt|cpt|ead|stem opt) ")
_CA_ONLY_STATUS = _js_regex(r" (pgwp|post graduation work permit|study permit|landed immigrant|pr card) ")


def countries_in(text: Optional[str]) -> list[Country]:
    """Countries a free-text statement names (by name, alias, or demonym), in the order found
    (TS: ``countriesIn``)."""
    text = text or ""
    norm = f" {_NOT_ALNUM.sub(' ', text.lower()).strip()} "
    found: list[Country] = []
    seen: set[str] = set()

    def add(country: Optional[Country]) -> None:
        if country and country.code not in seen:
            seen.add(country.code)
            found.append(country)

    # Multi-word and unambiguous names first.
    for pattern, code in _NAME_PATTERNS:
        if pattern.search(norm):
            add(_country_by_code(code))
    if _CAPITAL_US.search(text):
        add(_country_by_code("US"))
    # US-only statuses imply the US even when the country is not named.
    if _US_ONLY_STATUS.search(norm):
        add(_country_by_code("US"))
    # Canada-only statuses.
    if _CA_ONLY_STATUS.search(norm):
        add(_country_by_code("CA"))
    # Any other country named outright.
    for word in norm.strip().split(" "):
        # "america(n)" was judged above, with its continent guard.
        if len(word) < 4 or word in ("america", "american"):
            continue
        add(_country_from_name(word))
    return found


_DENIED = _js_regex(
    r"\b(not|no|never)\s+(legally\s+)?(authori[sz]ed|eligible|entitled|permitted|allowed)\b"
    r"|\bno work (authori[sz]ation|permit)\b"
    r"|\bnot a (citizen|resident)\b"
)
_CITIZEN = _js_regex(r"\bcitizen(ship)?\b|\bnational\b")
_PERMANENT_RESIDENT = _js_regex(
    r"\bpermanent resident\b|\bgreen card\b|\blanded immigrant\b|\blawful permanent\b|\bpr\b"
)
_STUDENT = _js_regex(r"\b(student visa|study permit|f-?1|j-?1|m-?1|international student)\b")
_SPONSORED_VISA = _js_regex(r"\b(h-?1b|l-?1|e-?3|o-?1|tn)\b")
_PERMIT = _js_regex(
    r"\b(work permit|open work permit|pgwp|post-graduation work permit|ead"
    r"|employment authori[sz]ation|work visa|opt|cpt)\b"
)
_AFFIRMATIVE = _js_regex(
    r"\b(authori[sz]ed|eligible|entitled|permitted|allowed|legally able|right) to work\b|\bcan (legally )?work\b"
)


# A negation's reach ends at the next clause. "and" ends it only before a new statement
# ("not in the US and authorized in Canada"); between two countries ("not in the US and the
# UK") it keeps both negated.
_CLAUSE_BREAK = _js_regex(
    r"[,;.!?()]|\b(but|however|whereas|while|although|though|yet)\b"
    r"|\band\s+(?=(i|am|is|authori[sz]ed|eligible|entitled|permitted|allowed|legally|can|have|hold|a|an)\b)"
)
# Words that put the countries after them outside the statement.
_NEGATION = _js_regex(r"\b(not|never|except|excluding|other than|outside( of)?)\b")
# A negated clause about sponsorship ("not requiring sponsorship in the US") says nothing
# against the country's authorization.
_SPONSORSHIP_WORDS = _js_regex(r"\b(sponsor\w*|requir\w*|need\w*)\b")
_STATUS_ARTICLE = _js_regex(r"^\s*(an?|the applicant an?)\s")
_STATUS_NOUN = _js_regex(r"\b(citizen|national|resident)\b")
_ONLY = _js_regex(r"\bonly\b")


def negated_country_scope(text: str) -> tuple[dict[str, bool], str]:
    """Split a work-authorization statement into the countries it negates and the text left
    (TS: ``negatedCountryScope``). Each negation word reaches to the next clause break:

    "Authorized to work in Canada but not in the US"  negated US (outright)
    "Not authorized to work in the US, but in Canada"  negated US (outright)
    "Canadian citizen, not a US citizen"               negated US (not outright)

    "Outright" means the clause excludes the country itself, not a status in it ("not a ...
    citizen / resident"), so the country is a No and not unknown. Returns
    ``(negated: code -> outright, positive_text)``.
    """
    negated: dict[str, bool] = {}
    lower = text.lower()
    cuts: list[tuple[int, int]] = []
    matches = list(_NEGATION.finditer(lower))
    for i, m in enumerate(matches):
        start = m.end()
        # A clause also ends at the next negation, which starts its own: the clauses never
        # overlap, so a long run of negations stays linear.
        limit = matches[i + 1].start() if i + 1 < len(matches) else len(lower)
        window = lower[start:limit]
        brk = _CLAUSE_BREAK.search(window)
        end = start + (brk.start() if brk else len(window))
        clause = lower[start:end]
        if _SPONSORSHIP_WORDS.search(clause):
            continue
        # Same indices in the original text: countries_in reads "US" by its capitals.
        named = countries_in(text[start:end])
        if not named:
            continue
        outright = not _STATUS_ARTICLE.search(clause) and not _STATUS_NOUN.search(clause)
        for c in named:
            negated[c.code] = negated.get(c.code, False) or outright
        cuts.append((m.start(), end))
    kept: list[str] = []
    at = 0
    for a, b in cuts:
        kept.append(text[at:a])
        at = b
    kept.append(text[at:])
    return negated, _trim(" ".join(kept))


def work_auth_facts(profile: ProfileInput) -> WorkAuthFacts:
    """Read the free-text work authorization (and the stated sponsorship answer) into per-country
    facts (TS: ``workAuthFacts``).

    "Canadian citizen"                          CA: authorized, no sponsorship
    "Authorized to work in Canada and the U.S." CA, US: authorized
    "Permanent resident of Canada"              CA: authorized, no sponsorship
    "F-1 student visa (OPT eligible)"           US: authorization unknown (restricted)
    "H-1B"                                      US: authorized, needs sponsorship
    "Yes"                                       unscoped: authorized

    The explicit per-country answers (authorizedUS / authorizedCanada) are applied last.
    """
    full_text = _trim(_text(profile, "workAuthorization"))
    by_country: dict[str, CountryAuth] = {}
    stated_sponsorship = polarity_of(_text(profile, "requiresSponsorship"))
    unscoped_authorized: Optional[bool] = None

    # "Canada but not in the US": the negated clause names countries that are never
    # authorized, and the rest of the sentence is read without it, so the status it states
    # belongs only to the countries named positively.
    negated, text = negated_country_scope(full_text)
    lower = text.lower()
    countries = [c for c in countries_in(text) if c.code not in negated]
    # A statement that limits itself ("only in Canada", "but not the US") is an authorization
    # statement for the countries it keeps.
    scoped = bool(negated) or bool(_ONLY.search(lower))

    denied = bool(_DENIED.search(lower))
    citizen = bool(_CITIZEN.search(lower))
    pr = bool(_PERMANENT_RESIDENT.search(lower))
    student = bool(_STUDENT.search(lower))
    sponsored_visa = bool(_SPONSORED_VISA.search(lower))
    permit = bool(_PERMIT.search(lower))
    affirmative = bool(_AFFIRMATIVE.search(lower))
    bare_yes = polarity_of(text)

    if not countries and not negated:
        if denied or bare_yes is False:
            unscoped_authorized = False
        elif affirmative or bare_yes is True or citizen or pr:
            unscoped_authorized = True
        _apply_explicit_countries(profile, by_country)
        return WorkAuthFacts(_ReadOnlyDict(by_country), stated_sponsorship, unscoped_authorized)

    for c in countries:
        if denied:
            by_country[c.code] = CountryAuth(False, None, "denied")
        elif citizen and not pr and not student and not permit:
            by_country[c.code] = CountryAuth(True, False, "citizen")
        elif pr and not student and not permit:
            by_country[c.code] = CountryAuth(True, False, "permanent_resident")
        elif sponsored_visa:
            by_country[c.code] = CountryAuth(True, True, "work_permit")
        elif student:
            by_country[c.code] = CountryAuth(None, None, "student")
        elif permit:
            by_country[c.code] = CountryAuth(True, None, "work_permit")
        elif affirmative or bare_yes is True or scoped:
            by_country[c.code] = CountryAuth(True, None, "statement")
    # A country the sentence excludes outright ("not in the US", "except the US") is a No.
    # One it only says the applicant is not a citizen of ("not a US citizen") stays unknown:
    # a green card holder says that too.
    for code, outright in negated.items():
        if outright:
            by_country[code] = CountryAuth(False, None, "denied")
    _apply_explicit_countries(profile, by_country)
    return WorkAuthFacts(_ReadOnlyDict(by_country), stated_sponsorship, unscoped_authorized)


def _apply_explicit_countries(profile: ProfileInput, by_country: dict[str, CountryAuth]) -> None:
    """Apply the applicant's explicit Yes/No per country, in place (TS: ``applyExplicitCountries``).

    These are the profile's "Authorized to work in the US / in Canada" answers. Each answers
    exactly the question an employer asks, so it wins over the free-text reading for that
    country. Not authorized there means sponsorship is needed to work there; authorized keeps
    what the text said about the future (OPT: authorized now, sponsored later). An answer
    that agrees with the text keeps the text's richer basis: a "Canadian citizen" who says
    Yes for Canada is still a citizen of Canada.
    """
    explicit = (
        ("US", _text(profile, "authorizedUS")),
        ("CA", _text(profile, "authorizedCanada")),
    )
    for code, stated in explicit:
        p = polarity_of(stated)
        if p is None:
            continue
        prev = by_country.get(code)
        if prev is not None and prev.authorized == p:
            continue
        # A Yes keeps what the status was: F-1 OPT is authorized AND a student visa, and
        # rewritten as "stated" it would no longer be known not to be citizenship.
        by_country[code] = CountryAuth(
            authorized=p,
            needs_sponsorship=(prev.needs_sponsorship if prev is not None else None) if p else True,
            basis=prev.basis if (p and prev is not None) else "stated",
        )


def _free_movement_citizen(auth: WorkAuthFacts, country_code: str) -> bool:
    """A citizen of a free-movement country asked about another one ("Are you legally
    authorized to work in Austria?" for a German citizen). TS: ``freeMovementCitizen``."""
    return country_code in FREE_MOVEMENT and any(
        a.basis == "citizen" and a.authorized is True and cc in FREE_MOVEMENT for cc, a in auth.by_country.items()
    )


def authorized_in(auth: WorkAuthFacts, country_code: Optional[str], residence: Optional[str]) -> Optional[Fact[bool]]:
    """Is the applicant authorized to work in ``country_code``? (TS: ``authorizedIn``)

    ``country_code`` None (or "") means the question names no country. ``residence`` is the
    country the applicant lives in (``residence_of``), or None. High-confidence answers only
    come from a statement that covers that country; None means the profile does not say.
    """
    if country_code:
        c = auth.by_country.get(country_code)
        if c is not None and c.authorized is not None:
            return Fact(c.authorized, "high", f"work-auth:{c.basis}")
        if c is None and _free_movement_citizen(auth, country_code):
            return Fact(True, "high", "work-auth:free-movement")
        # A statement that named no country, about a question that names the applicant's own
        # country of residence: the statement was made about the place they live and apply.
        if not auth.by_country and auth.unscoped_authorized is not None and residence == country_code:
            return Fact(auth.unscoped_authorized, "high", "work-auth:unscoped-residence")
        return None
    # No country in the question.
    if not auth.by_country and auth.unscoped_authorized is not None:
        return Fact(auth.unscoped_authorized, "high", "work-auth:unscoped")
    return None


def needs_sponsorship_in(
    auth: WorkAuthFacts, country_code: Optional[str], residence: Optional[str]
) -> Optional[Fact[bool]]:
    """Does the applicant need sponsorship (now or in future) to work in ``country_code``?
    (TS: ``needsSponsorshipIn``)"""
    stated = auth.stated_sponsorship
    if country_code:
        c = auth.by_country.get(country_code)
        # Not authorized there: working there takes sponsorship, whatever the general answer
        # says (a Canadian's "No" is about Canada).
        if c is not None and c.authorized is False:
            return Fact(True, "high", f"sponsorship:not-authorized-{c.basis}")
        # A citizen or permanent resident never needs sponsorship there, whatever the general
        # answer says.
        if c is not None and c.basis in ("citizen", "permanent_resident"):
            return Fact(False, "high", f"sponsorship:{c.basis}")
        if c is None and _free_movement_citizen(auth, country_code):
            return Fact(False, "high", "sponsorship:free-movement")
        # The applicant's own answer wins for any country their status covers.
        if stated is not None and (
            c is not None or (not auth.by_country and (not residence or residence == country_code))
        ):
            return Fact(stated, "high", "sponsorship:stated")
        if c is not None and c.needs_sponsorship is not None:
            return Fact(c.needs_sponsorship, "high", f"sponsorship:{c.basis}")
        return None
    # No country in the question: only the applicant's own answer settles it, and only when
    # their status is not confined to some OTHER country. Not authorized somewhere, the
    # question may well be about that place.
    if stated is not None and any(c.authorized is False for c in auth.by_country.values()):
        return Fact(stated, "medium", "sponsorship:stated-unscoped-not-everywhere")
    if stated is not None:
        if not auth.by_country or (residence and residence in auth.by_country) or len(auth.by_country) == 1:
            return Fact(stated, "high", "sponsorship:stated-unscoped")
        return Fact(stated, "medium", "sponsorship:stated-multi-country")
    if len(auth.by_country) == 1:
        (only,) = auth.by_country.values()
        if only.needs_sponsorship is False:
            return Fact(False, "medium", f"sponsorship:{only.basis}-unscoped")
    return None
