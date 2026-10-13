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
    * (Changed 2026-10-13 with the extension, decide-b round 3.) Work authorisation is read
      the binding way: a country gets a status, a Yes or a No only from a pattern bound
      directly to it ("US citizen", "citizen of Canada", "authorized to work in the US",
      "US: no", "Yes for Canada", "green card"); a country the text only names ("living in
      Canada") stays unknown. chrome-extension/test/fixtures/work-auth-cases.json holds the
      expected readings both sides are graded against.
    * "N/A", "Not applicable", "Not sure" and "Not yet" are unanswered, never No.
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
    "collapse_spaces",
    "countries_in",
    "country_of",
    "location_facts",
    "needs_sponsorship_in",
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
    #: The applicant wrote a sponsorship answer that reads neither Yes nor No ("No for Canada,
    #: yes for US"): nothing may be inferred in its place.
    sponsorship_unclear: bool = False


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

_YES_PREFIX = _js_regex(r"^(yes|y|yeah|yep|yup|true|oui|si)\b")
_NO_PREFIX = _js_regex(r"^(no|n|nope|nah|false|non|none|not)\b")
_WHITESPACE_RUN = _js_regex(r"\s+")
_LINE_BREAK = re.compile(r"[\n\r]")
# A leading question echoed before the answer (TS: ``QUESTION_ECHO``).
_QUESTION_ECHO = _js_regex(r"^(do|does|are|is|will|would|have|has|can|could)\b")


def collapse_spaces(text: str) -> str:
    """Every run of whitespace as one character: a newline when the run holds a line break (a
    labelled answer keeps its lines), else a space (TS: ``collapseSpaces``). Applied to user text
    before any other pattern (decide-b round 4), so no pattern ever meets a long whitespace run."""
    return _WHITESPACE_RUN.sub(lambda m: "\n" if _LINE_BREAK.search(m.group(0)) else " ", text)
_NEGATED_NEED = _js_regex(r"\b(will not|won't|do not|don't|does not|doesn't|no longer|never)\s+(need|require)")
_PLAIN_NEED = _js_regex(r"\b(will|do|does|would)\s+(need|require)\b")
_NOT_REQUIRED = _js_regex(r"\b(not required|not needed)\b")
_REQUIRED = _js_regex(r"\b(required|needed)\b")
# "N/A" and its spellings (TS: ``NOT_APPLICABLE``): unanswered, never a No.
_NOT_APPLICABLE = _js_regex(r"^(n\s*[/.]?\s*a\b\.?|not applicable\b|does not apply\b|doesn'?t apply\b)")
# An answer that says the applicant does not know (TS: ``UNCERTAIN_ANSWER``).
_UNCERTAIN_ANSWER = _js_regex(r"^(not sure|unsure|not certain|uncertain|unknown|tbd|don'?t know|do not know)\b")
# Words that put an answer in the future (TS: ``FUTURE_WORDS``).
_FUTURE_WORDS = _js_regex(r"\b(will|later|future|after|eventually)\b")
_NOT_NOW = _js_regex(r"^not (yet|currently|presently|now|at the moment|right now|at this time|for now)\b")
_UNTIL = _js_regex(r"\buntil\b")
_YES_WORD = _js_regex(r"\byes\b")
_NEGATED_FUTURE = _js_regex(r"\b(will not|won't|never)\b")


def polarity_of(text: Optional[str]) -> Optional[bool]:
    """Yes/no reading of a short stored answer, or None. Blank is None, never False (TS: ``polarityOf``).

    "N/A", "NA", "N.A.", "not applicable" are None: unanswered, never a No (owner decisions
    2026-10-13).
    """
    t = _trim(collapse_spaces(text or "")).lower()
    if not t:
        return None
    # "Do you now or in the future require sponsorship? No.": the answer follows the echo.
    q = t.rfind("?")
    if q >= 0 and _QUESTION_ECHO.search(t):
        rest = _trim(t[q + 1:])
        if rest:
            t = rest
    if _NOT_APPLICABLE.search(t):
        return None
    # "Not sure", "TBD": unanswered. Never a No (decide-b review I-2).
    if _UNCERTAIN_ANSWER.search(t):
        return None
    if _YES_PREFIX.search(t):
        return True
    if _NO_PREFIX.search(t):
        # "Not yet", "Not currently, but I will", "Not required now but will be after OPT":
        # a No today that turns into a Yes later, so neither.
        # "Not at the moment", "Not right now": only about today.
        if _NOT_NOW.search(t):
            return None
        # "No (will need after OPT)", "No, until my OPT ends", "No for Canada, yes for US": a
        # later need or a later Yes. "No, I will not need it in the future" stays a No.
        if _UNTIL.search(t) or _YES_WORD.search(t[2:]):
            return None
        if _FUTURE_WORDS.search(t) and not _NEGATED_FUTURE.search(t):
            return None
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
    r"\b(not|no|never)\s+((yet|currently|presently)\s+)?(legally\s+)?(authori[sz]ed|eligible|entitled|permitted"
    r"|allowed)\b"
    r"|\bno work (authori[sz]ation|permit)\b"
)
# "Not a citizen" denies nothing for a permanent resident, who is authorized without being one.
_NOT_A_CITIZEN = _js_regex(r"\bnot a (citizen|resident)\b")
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


# ---------------------------------------------------------------------------
# Reading a free-text work authorization the binding way (decide-b round 3; TS: ``readWorkAuth``)
# ---------------------------------------------------------------------------

#: A status that settles sponsorship for the country (none needed), or a No.
_SETTLED = frozenset({"citizen", "permanent_resident", "denied"})

# "N/A - citizen": not applicable because of a settled status (TS: ``NA_BECAUSE_SETTLED``).
_NA_BECAUSE_SETTLED = _js_regex(
    r"^(?:n ?[/.]? ?a\b\.?|not applicable\b)[^a-z0-9]{0,4}(?:i'?m |i am )?(?:an? )?(?:[a-z]+ )?"
    r"(?:citizen|permanent resident)\b"
)
# Words that move a leading No to another time (TS: ``NOT_TODAY``).
_NOT_TODAY = _js_regex(r"\b(?:but|later|future|eventually|after|until|yet|currently|now)\b")
_LEADING_NO = _js_regex(r"^(no|nope|n)\b")

# Misspellings of the status words, read as the word (TS: ``TYPOS``).
_TYPOS = (
    (_js_regex(r"\b(?:citizan|citzen|citizn|citezen|citisen|citizon)(s|ship)?\b", re.IGNORECASE), "citizen"),
    (_js_regex(r"\b(?:canadain|candian|canadien|canadan|canandian)\b", re.IGNORECASE), "canadian"),
    (_js_regex(r"\b(?:amercian|amerian|amrican)\b", re.IGNORECASE), "american"),
    (_js_regex(r"\b(?:authroized|autorized|athorized|authorzed|authorizd|authoirzed|authozied|authrized|autherized"
               r"|authorixed)\b", re.IGNORECASE), "authorized"),
    (_js_regex(r"\b(?:elligible|eligable|elegible)\b", re.IGNORECASE), "eligible"),
    (_js_regex(r"\b(?:permenant|permanant|perminant)\b", re.IGNORECASE), "permanent"),
)


def _normalize_answer(text: str) -> str:
    """The work-authorization answer as the patterns read it: whitespace runs collapsed (first,
    before any other pattern) and status typos corrected (TS: ``normalizeAnswer``)."""
    t = _trim(collapse_spaces(text))
    for pattern, word in _TYPOS:
        t = pattern.sub(lambda m, word=word: word + (m.group(1) or "") if m.re.groups else word, t)
    return t


@dataclass
class _Bound:
    """What the text binds to one country (TS: ``Bound``)."""

    citizen: bool = False
    notCitizen: bool = False
    pr: bool = False
    student: bool = False
    sponsoredVisa: bool = False
    permit: bool = False
    affirmative: bool = False
    deny: bool = False
    sponsored: bool = False


@dataclass(frozen=True)
class _StatusFlags:
    """The work-right statuses a piece of text states, with no country (TS: ``StatusFlags``)."""

    citizen: bool
    pr: bool
    student: bool
    sponsored_visa: bool
    permit: bool
    affirmative: bool


def _status_flags_of(t: str) -> _StatusFlags:
    """TS: ``statusFlagsOf``."""
    return _StatusFlags(
        citizen=bool(_CITIZEN.search(t)),
        pr=bool(_PERMANENT_RESIDENT.search(t)),
        student=bool(_STUDENT.search(t)),
        sponsored_visa=bool(_SPONSORED_VISA.search(t)),
        permit=bool(_PERMIT.search(t)),
        affirmative=bool(_AFFIRMATIVE.search(t)),
    )


_ABBREVIATION_PERIOD = _js_regex(r"\b([a-z])\.(?=\s?[a-z]\.)|\bu\.(?=\s?s\b)")
_ABBREVIATION_END = _js_regex(r"[a-z]_\s?[a-z]\.")
_NEW_SENTENCE = _js_regex(r"^\s+[A-Z]")
# Words that continue a phrase after "U.S." ("U.S. Citizen", "U.S. Permanent Resident").
_ABBREVIATION_CONTINUES = _js_regex(
    r"^\s+(citizen\w*|national\w*|resident\w*|permanent|green|work\w*|visa|employment|employer\w*|passport"
    r"|person|government|based|company|companies)\b"
)


def _mask_abbreviations(text: str) -> str:
    """The lowercase text with the periods inside an abbreviation ("u.s.", "u. s.", "u.s.a.",
    "u.k.") turned into "_", so a period ends a unit only where a sentence does (TS:
    ``maskAbbreviations``). Same length, so indices still match.

    The last period of a dotted abbreviation ends a sentence only when a new one starts after
    it, which the ORIGINAL text shows by a capital: "Not authorized in the U.S. I am a Canadian
    citizen" ends there; "U.S. citizen" and "U.S. Citizen" do not.
    """
    lower = text.lower()
    inner = _ABBREVIATION_PERIOD.sub(lambda m: m.group(0).replace(".", "_", 1), lower)

    def last(m: re.Match[str]) -> str:
        end = m.end()
        new_sentence = bool(_NEW_SENTENCE.search(text[end:end + 40])) and not _ABBREVIATION_CONTINUES.search(
            inner[end:end + 40])
        return m.group(0) if new_sentence else m.group(0)[:-1] + "_"

    return _ABBREVIATION_END.sub(last, inner)


def _country_keys() -> list[str]:
    """Every key that names a country: names and aliases of four letters or more, "us", "usa",
    "uk", "uae" and "the states", longest first (TS: ``COUNTRY_KEYS``)."""
    keys: set[str] = set()
    for c in COUNTRIES:
        for k in (c.name, *c.aliases):
            n = _geo_norm(k)
            if n == "states":
                continue
            if len(n) >= 4 or n in ("us", "usa", "uk", "uae"):
                keys.add(n)
    keys.add("the states")
    return sorted(keys, key=lambda k: (-len(k), k))


def _key_source(k: str) -> str:
    """A key as a pattern. "America" is the US only when no continent word precedes it
    (TS: ``keySource``)."""
    src = k.replace(" ", r"\s+")
    if k in ("america", "american"):
        return r"(?<!north\s)(?<!south\s)(?<!latin\s)(?<!central\s)" + src
    return src


_COUNTRY_KEYS = tuple(_country_keys())
_COUNTRY_ALT = r"(?:u_\s?s(?:_\s?a)?_?|u_\s?k_?|" + "|".join(_key_source(k) for k in _COUNTRY_KEYS) + ")"
_C = r"\b" + _COUNTRY_ALT + r"\b"
# At most eight countries in one list (TS: ``LIST``): a longer one costs O(n^2).
_LIST = _C + r"(?:\s*(?:&|\/|\+|\band\b|\bor\b)\s*(?:the\s+)?" + _C + "){0,7}"
_COUNTRY_RE = _js_regex(_C)
# In the text the patterns read, every country name is replaced by a run of MARK of the same
# length (TS: ``MARK``): the patterns then test one character instead of every name.
_MARK = "\u00a7"
_M = _MARK + "+"
# A list starts at the start of a name, never again inside the same run of marks (TS: ``MLIST``).
_MLIST = "(?<!" + _MARK + ")" + _M + r"(?:\s*(?:&|\/|\+|\band\b|\bor\b)\s*(?:the\s+)?" + _M + "){0,7}"
_MARK_RUN = re.compile(_M)


def _demonyms() -> frozenset[str]:
    """TS: ``DEMONYMS``."""
    out = {"swiss", "greek", "filipino", "kiwi"}
    ending = _js_regex(r"(an|ish|ese|i|ch)$")
    for c in COUNTRIES:
        name = _geo_norm(c.name)
        for a in c.aliases:
            n = _geo_norm(a)
            if n != name and ending.search(n):
                out.add(n)
    return frozenset(out)


_DEMONYMS = _demonyms()
_JS_SPACES = _js_regex(r"\s+")


def _code_of_key(match: str) -> Optional[str]:
    """The country a matched key names (TS: ``codeOfKey``)."""
    key = _trim(_JS_SPACES.sub(" ", match.replace("_", " ")))
    if key == "the states":
        return "US"
    country = _country_from_name(key)
    return country.code if country else None


# Visas and statuses that only exist in one country (TS: ``VISA``).
_VISA = (
    r"h-?1-?b|tn|l-?1a?|e-?3|o-?1|ead|stem\s+opt|opt|cpt|f-?1|j-?1|m-?1|green\s*card|gc|pgwp"
    r"|post[\s-]?grad(?:uate|uation)?\s+work\s+permit|pr"
    r"|permanent\s+residen(?:ce|cy|t)|work\s+permit|citizenship|refugee\s+status|protected\s+person"
)
# Needing, applying for, waiting on, being able or planning to get a visa is not holding it; nor is
# a visa that is eligible, pending, in process, in a lottery or starting later, the pending word
# possibly across one spaced dash, colon or bracket (TS: ``VISA_INTENT``).
_VISA_INTENT = _js_regex(
    r"\b(?:need|needs|needed|require|requires|required|would\s+need|will\s+need|will\s+get|will\s+apply\s+for"
    r"|apply\s+for|applying\s+for|applied\s+for|waiting\s+(?:on|for)|awaiting|hoping\s+(?:to\s+get|for)"
    r"|eligible\s+for|pending"
    r"|(?:can|could|would|might|may)\s+(?:get|obtain|apply\s+for|qualify\s+for)|eligible\s+to\s+(?:get|obtain|apply\s+for)"
    r"|qualify\s+for|plan(?:s|ning)?\s+(?:to\s+(?:get|obtain|apply\s+for)|on\s+(?:getting|applying\s+for))"
    r"|intend(?:s|ing)?\s+to\s+(?:get|obtain|apply\s+for)|going\s+to\s+(?:get|apply\s+for))"
    r"\s+(?:an?\s+|my\s+|the\s+)?(?:(?:the\s+)?" + _LIST + r"\s+)?(?:" + _VISA + r")\b"
    r"(?:\s+(?:visa|status|application|approval|petition))?"
    r"|\b(?:relocat\w*|mov(?:e|es|ed|ing))\s+to\s+(?:the\s+)?" + _LIST + r"\s+(?:on|with|via|using|through|under)\s+"
    r"(?:an?\s+|the\s+)?(?:" + _VISA + r")\b(?:\s+(?:visa|status))?"
    r"|\b(?:" + _VISA + r")\b(?:\s(?:visa|status|application|card))?(?:\s?[-–—:(\[]\s?|\s|-)"
    r"(?:eligible|eligibility|application|approval|petition|pending|in\s+progress|in\s+process|processing|starting"
    r"|begins?|start\s+date|lottery|applied|submitted)\b"
)


def _mask_visa_intent(norm: str) -> str:
    return _VISA_INTENT.sub(lambda m: " " * len(m.group(0)), norm)


# Ends of a unit; the weak ones still let a bare answer and a bare country list pair up.
_UNIT_BREAK = _js_regex(
    r"\r\n|\n|\r|;|\||\.(?=\s|$)|!|\?|,|\(|\)|\[|\]|\s[-\u2013\u2014]+\s|\s\/\s"
    r"|\b(?:but|however|whereas|while|although|though)\b"
)
# \Z, not $: a break can end in "\n", and Python's $ matches before a final "\n" (JavaScript's
# does not).
_WEAK_BREAK = _js_regex(r"^(?:,|\(|\)|\[|\]|\s[-\u2013\u2014]+\s)\Z")
_ANSWER_WORD = _js_regex(
    r"^\s*(?:(yes|y|yeah|yep|oui|si|[\u2705\u2714\u2611\u2713])|(no|n|nope|non|[\u274c\u2717\u2718\u2716]))\s*$"
)
_PURE_LIST = _js_regex(r"^\s*(?:(?:only\s+)?(?:in|for)\s+)?(?:the\s+)?(" + _MLIST + r")\s*(?:only\s*)?(?::\s*)?$")
_STATUS_ONLY_CITIZEN = _js_regex(r"\b(?:dual\s+)?citizen\w*\b|\bnationals?\b")
_STATUS_ONLY_PR = _js_regex(r"\bpermanent\s+resident\w*|\bperm(?:anent)?\.?\s+res\w*|\bpr\b")
_STATUS_ONLY_PERMIT = _js_regex(r"\b(?:open\s+)?work\s+(?:permit|visa)\b")
_SPONSOR_NEED = _js_regex(
    r"\b(?:need|needs|require|requires|requiring|would\s+need|will\s+need|would\s+require|will\s+require)\s+"
    r"(?:a\s+|an\s+)?(?:visa\s+|work\s+)?(?:sponsorship|sponsor|visa)\b"
)
_SPONSOR_NEGATED = _js_regex(
    r"\b(?:not|no|never|without|do\s+not|does\s+not|will\s+not|don['\u2019]?t|doesn['\u2019]?t|won['\u2019]?t)"
    r"\s+(?:\w+\s+){0,2}?(?:need|require|requiring|sponsor)"
)

_YES_MARK = r"[\u2705\u2714\u2611\u2713]"
_NO_MARK = r"[\u274c\u2717\u2718\u2716]"
_AUTH_ADJ = r"(?:authori[sz]ed|eligible|entitled|permitted|allowed|legally\s+able|able|free|cleared)"
_AUTH_NOUN = r"(?:authori[sz]ation|auth|permit|status|visa|eligibility)"
_L = "(" + _MLIST + ")"

# TS: ``POSTFIX_PATTERNS``, ``PREFIX_ANSWER_PATTERNS``, ``PATTERNS``, in the same order.
_POSTFIX_PATTERNS = (
    (_js_regex(_L + r"\s*(?:work\s+(?:authori[sz]ation|auth|permit)\s*)?(?::|-|\u2013)?\s*(?:\b(?:yes|oui)\b|"
               + _YES_MARK + ")"), "affirmative"),
    (_js_regex(_L + r"\s*(?:work\s+(?:authori[sz]ation|auth|permit)\s*)?(?::|-|\u2013)?\s*(?:\bno\b"
               r"|\bnone\b(?!\s(?:needed|required|necessary))|\bnot\s+"
               r"(?:(?:yet|currently)\s+)?(?:legally\s+)?(?:authori[sz]ed|eligible|allowed|permitted)\b|" + _NO_MARK + ")"),
     "deny"),
)
_PREFIX_ANSWER_PATTERNS = (
    (_js_regex(r"(?:\b(?:yes|oui)\b|" + _YES_MARK + r")\s*(?:[:\-\u2013]\s*)?(?:(?:for|in)\s+)?(?:the\s+)?" + _L),
     "affirmative"),
    (_js_regex(r"(?:\bno\b|" + _NO_MARK + r")\s*(?:[:\-\u2013]\s*)?(?:(?:for|in)\s+)?(?:the\s+)?" + _L), "deny"),
)
_PATTERNS = (
    # Denials.
    (_js_regex(r"\b(?:not|never)\s+(?:(?:yet|currently|presently)\s+)?(?:legally\s+)?" + _AUTH_ADJ
               + r"\s+(?:\(?yet\)?\s+)?(?:to\s+(?:legally\s+)?work\s+)?(?:(?:in|for|within)\s+)?(?:the\s+)?" + _L),
     "deny"),
    (_js_regex(r"\b(?:except|excluding|other\s+than|outside(?:\s+of)?)\s+(?:for\s+)?(?:in\s+)?(?:the\s+)?" + _L),
     "deny"),
    (_js_regex(r"(?:\bcannot|\bcan['\u2019]?t|\bcan\s+not|\bunable\s+to|\bnot\s+able\s+to)\s+(?:legally\s+)?work\s+"
               r"(?:in|for)\s+(?:the\s+)?" + _L), "deny"),
    (_js_regex(r"\b(?:ineligible|unauthori[sz]ed)\s+(?:to\s+work\s+)?(?:(?:in|for)\s+)?(?:the\s+)?" + _L), "deny"),
    (_js_regex(r"(?:\bno|\bwithout|\b(?:do|does)\s+not\s+(?:currently\s+|yet\s+)?have"
               r"|\b(?:don|doesn)['\u2019]?t\s+(?:currently\s+|yet\s+)?have|" + _NO_MARK
               + r")\s*(?:an?\s+|any\s+)?(?:the\s+)?" + _L + r"\s+(?:work\s+)?" + _AUTH_NOUN), "deny"),
    (_js_regex(r"(?:\bno|\bwithout|\b(?:do|does)\s+not\s+(?:currently\s+|yet\s+)?have"
               r"|\b(?:don|doesn)['\u2019]?t\s+(?:currently\s+|yet\s+)?have)\s+(?:an?\s+|any\s+)?"
               r"(?:work\s+)?(?:authori[sz]ation|permit|right|status)\s+(?:to\s+work\s+)?(?:in|for)\s+(?:the\s+)?" + _L),
     "deny"),
    # "not a citizen of the US", "US citizen not"
    (_js_regex(r"\bnot\s+(?:an?\s+)?(?:citizen\w*|nationals?|resident\w*)\s+of\s+(?:the\s+)?" + _L), "notCitizen"),
    (_js_regex(_L + r"\s+citizen\w*\s+(?:not|no)\s*$"), "notCitizen"),
    # Citizenship: "US citizen", "Canada: citizen", "citizen of (both) Canada and the US".
    (_js_regex(_L + r"\s+(?:dual\s+)?(?:citizen\w*|nationals?)\b"), "citizen"),
    (_js_regex(_L + r"\s?:\s?(?:dual\s+)?(?:citizen|citizenship|national)\b(?!\s(?:of|in)\b)"), "citizen"),
    (_js_regex(r"\b(?:dual\s+)?(?:citizen\w*|nationals?)\s*(?:of|:|-|\u2013)\s*(?:both\s+)?(?:the\s+)?" + _L), "citizen"),
    # Permanent residence: "US permanent resident", "Canada: PR", "PR of Canada".
    (_js_regex(_L + r"\s+(?:permanent\s+resident\w*|perm(?:anent)?\.?\s+res(?:ident)?\w*|pr|lpr|landed\s+immigrant)\b"),
     "pr"),
    (_js_regex(_L + r"\s?:\s?(?:permanent\s+resident|pr|lpr|landed\s+immigrant)\b(?!\s(?:of|in)\b)"), "pr"),
    (_js_regex(r"\b(?:permanent\s+resident\w*|perm(?:anent)?\.?\s+res(?:ident)?\w*|pr|lpr)\s*(?:of|in|:|-|\u2013)\s*"
               r"(?:the\s+)?" + _L), "pr"),
    # Authorized to work there.
    (_js_regex(r"\b" + _AUTH_ADJ + r"\s+(?:to\s+(?:legally\s+)?work\s+)?(?:only\s+)?(?:in|for|within)\s+(?:both\s+)?"
               r"(?:the\s+)?" + _L), "affirmative"),
    (_js_regex(r"\bcan\s+(?:legally\s+)?work\s+(?:only\s+)?(?:in|for)\s+(?:both\s+)?(?:the\s+)?" + _L), "affirmative"),
    (_js_regex(r"\bright\s+to\s+work\s+in\s+(?:the\s+)?" + _L), "affirmative"),
    (_js_regex(r"\bonly\s+(?:(?:in|for)\s+)?(?:the\s+)?" + _L), "affirmative"),
    (_js_regex(_L + r"\s+only\b"), "affirmative"),
    # A permit or student visa named for a country.
    (_js_regex(_L + r"\s+(?:open\s+)?(?:work\s+permit|work\s+visa)"), "permit"),
    (_js_regex(_L + r"\s?:\s?(?:an?\s)?(?:open\s+)?(?:work\s+permit|work\s+visa)"), "permit"),
    (_js_regex(r"\b(?:open\s+)?(?:work\s+permit|work\s+visa)\s*(?:in|for|of|:|-)\s*(?:the\s+)?" + _L), "permit"),
    # "Refugee status in Canada", "protected person in Canada": allowed to work.
    (_js_regex(r"\b(?:refugee\s+status|protected\s+person(?:\s+status)?)\s+(?:in|of)\s+(?:the\s+)?" + _L), "permit"),
    (_js_regex(_L + r"\s+(?:student\s+visa|study\s+permit)"), "student"),
    (_js_regex(_L + r"\s?:\s?(?:an?\s)?(?:student\s+visa|study\s+permit|international\s+student|student)\b"), "student"),
    (_js_regex(r"\b(?:student\s+visa|international\s+student)\s+(?:in|for)\s+(?:the\s+)?" + _L), "student"),
    # Sponsorship needed there: "need sponsorship for the US", "need an LMIA to work in Canada", "US
    # requires sponsorship", "US: need visa", "USA: sponsorship required".
    (_js_regex(r"\b(?:would\s+need|will\s+need|would\s+require|will\s+require|need|needs|needed|require|requires"
               r"|required|requiring)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:visa\s+|work\s+)?(?:sponsorship|sponsor|visa|lmia)\s+"
               r"(?:to\s+work\s+)?(?:for|in)\s+(?:the\s+)?" + _L), "sponsored"),
    (_js_regex(_L + r"(?:\s?:)?\s(?:\w+\s)?(?:requires?|needs?|requiring)\s(?:a\s|an\s)?(?:visa\s)?(?:sponsorship|visa)"),
     "sponsored"),
    (_js_regex(_L + r"(?:\s?:)?\s(?:visa\s)?sponsorship\s(?:is\s)?(?:required|needed)\b"), "sponsored"),
)
# Bound to Canada wherever it is said: an LMIA exists only there (TS: ``LMIA_NEED``).
_LMIA_NEED = _js_regex(r"\b(?:need|needs|needed|require|requires|required|requiring)\s(?:an?\s)?(?:new\s)?lmia\b")
# Needing an employer-sponsored US visa, read before the visa is masked (TS: ``SPONSORED_VISA_NEED``).
_SPONSORED_VISA_NEED = _js_regex(
    r"\b(?:need|needs|needed|require|requires|required|requiring|would\s(?:need|require)|will\s(?:need|require))\s"
    r"(?:an?\s|the\s|my\s)?(?:new\s)?(?:h-?1-?b|l-?1a?|e-?3|o-?1)\b"
)
# A unit that is one demonym and nothing that changes it (TS: ``DEMONYM_UNIT``).
_DEMONYM_UNIT = _js_regex(r"^\s?(?:i['’]?m\s|im\s|i\sam\s)?(?:an?\s)?(" + _M + r")\s?(?:$|(?:living|residing|based|who)\b)")
# A status-only unit in brackets after a single country (TS: ``SINGLE_COUNTRY_AT_END``).
_SINGLE_COUNTRY_AT_END = _js_regex(r"(" + _MLIST + r")\s?(?::\s?(?:yes|oui)\s?)?$")
# A unit that is only a denial: "not yet authorized" (TS: ``DENIAL_ONLY``).
_DENIAL_ONLY = _js_regex(
    r"^\s?not\s(?:(?:yet|currently)\s)?(?:legally\s)?(?:authori[sz]ed|eligible|allowed|permitted)(?:\sto\swork)?\s?$"
)
# Intent or plan words: a permit "with" them is not one held (TS: ``INTENT_WORDS``).
_INTENT_WORDS = _js_regex(
    r"\b(?:plan\w*|hop(?:e|es|ing)|want\w*|would|will|intend\w*|looking|seek(?:s|ing)?|wish(?:es|ing)?|apply(?:ing)?)\b"
)
# A letter or an answer mark: a unit without one binds nothing to its countries (TS: ``WORD_OR_MARK``).
_WORD_OR_MARK = re.compile("[a-z✅✔☑✓❌✗✘✖]")
# "in Canada with an open work permit" (TS: ``PERMIT_WITH``).
_PERMIT_WITH = _js_regex(r"\bin\s(?:the\s)?(" + _MLIST + r")\swith\s(?:an?\s)?(?:open\s)?work\s(?:permit|visa)\b")
# "not in the US", "not the U.S.", "not a US citizen", "not Canadian" (TS: ``NOT_PLACE``).
_NOT_PLACE = _js_regex(
    r"\b(?:not|never)\s+(?:(?:yet|currently|presently)\s+)?(?:(?:in|for|within)\s+)?(an?\s+)?(?:the\s+)?(" + _MLIST
    + r")(\s+(?:citizen\w*|nationals?|person|passport|resident\w*))?"
)
# Statuses that belong to one country whatever the text says around them (TS: ``INHERENT``).
_INHERENT = (
    (_js_regex(r"\b(?:h-?1-?b|tn|l-?1a?|e-?3|o-?1)\b"), "US", "sponsoredVisa"),
    (_js_regex(r"\b(?:ead|stem\s+opt|opt|cpt)\b"), "US", "permit"),
    (_js_regex(r"\b(?:f-?1|j-?1|m-?1)\b"), "US", "student"),
    (_js_regex(r"\b(?:green\s*card|gc|lpr)\b"), "US", "pr"),
    (_js_regex(r"\b(?:pgwp|post[\s-]?grad(?:uate|uation)?\s+work\s+permit)\b"), "CA", "permit"),
    (_js_regex(r"\bstudy\s+permit\b"), "CA", "student"),
    (_js_regex(r"\b(?:pr\s+card|landed\s+immigrant)\b"), "CA", "pr"),
)
_PR_AFTER = _js_regex(r"^(?:permanent|perm|pr|lpr)")
_LIST_AT_END = _js_regex(_M + r"\s+$")
# Before and after a lowercase "us" that is the country (TS: ``LOWERCASE_US_BEFORE`` / ``_AFTER``).
_LOWERCASE_US_BEFORE = _js_regex(r"\b(?:in|for|the|to|from|of)\s+$")
_LOWERCASE_US_AFTER = _js_regex(
    r"^\s*(?:citizen|national|pr\b|permanent|green|gc\b|person|work|employment|jobs?\b|roles?\b|visa|passport|based"
    r"|-|resident|:|yes\b|no\b|only\b|requires?\b|needs?\b|[\u2705\u2714\u2611\u2713\u274c\u2717\u2718\u2716])"
)
_QUESTION = re.compile(r"\?")
_CLOSING_BRACKET = _js_regex(r"^\s?\)")
_SPONSOR_WORD = _js_regex(r"\bsponsor")
_DENIED_UNSCOPED = _js_regex(
    r"\b(not|no|never)\s+((yet|currently|presently)\s+)?(legally\s+)?(authori[sz]ed|eligible|entitled|permitted"
    r"|allowed)\b|\bno work (authori[sz]ation|permit)\b"
)


@dataclass
class _Unit:
    start: int
    end: int
    #: The break after this unit up to the next one.
    break_after: str = ""


@dataclass
class _Reading:
    bound: dict[str, _Bound]
    mentioned: dict[str, None]
    sponsorship_no_country: bool


def _read_work_auth(text: str) -> _Reading:
    """Read a free-text work authorization the binding way (TS: ``readWorkAuth``).

    A country gets a status, a Yes or a No only from a pattern bound directly to it ("US
    citizen", "citizen of Canada", "authorized to work in the US", "US: no", "Yes for Canada",
    "green card"). A country the text only names ("living in Canada", "US-based") gets nothing,
    so it stays unknown.
    """
    bound: dict[str, _Bound] = {}
    mentioned: dict[str, None] = {}
    sponsorship_no_country = False

    def bind(codes: list[str], key: str) -> None:
        for code in codes:
            b = bound.get(code)
            if b is None:
                b = _Bound()
                bound[code] = b
            setattr(b, key, True)
            mentioned[code] = None

    unmasked = _mask_abbreviations(text)
    masked = _mask_visa_intent(unmasked)

    names: dict[int, tuple[str, str]] = {}
    pieces: list[str] = []
    last = 0
    for m in _COUNTRY_RE.finditer(masked):
        at = m.start()
        code = _code_of_key(m.group(0))
        if not code:
            continue
        names[at] = (code, _JS_SPACES.sub(" ", m.group(0)))
        pieces.append(masked[last:at])
        pieces.append(_MARK * len(m.group(0)))
        last = m.end()
        if (m.group(0) == "us" and text[at:at + 2] != "US"
                and not _LOWERCASE_US_BEFORE.search(masked[max(0, at - 8):at])
                and not _LOWERCASE_US_AFTER.search(masked[at + 2:at + 24])):
            continue
        mentioned[code] = None
    pieces.append(masked[last:])
    norm = "".join(pieces)

    def codes_at(start: int, end: int) -> list[str]:
        out: list[str] = []
        for m in _MARK_RUN.finditer(norm[start:end]):
            name = names.get(start + m.start())
            if name and name[0] not in out:
                out.append(name[0])
        return out

    for pattern, code, key in _INHERENT:
        if pattern.search(masked):
            bind([code], key)

    units: list[_Unit] = []
    at = 0
    pending = ""
    for m in _UNIT_BREAK.finditer(norm):
        idx = m.start()
        if _trim(norm[at:idx]):
            if units:
                units[-1].break_after = pending
            units.append(_Unit(at, idx))
            pending = ""
        pending += m.group(0)
        at = m.end()
    if _trim(norm[at:]):
        if units:
            units[-1].break_after = pending
        units.append(_Unit(at, len(norm)))

    paired: set[int] = set()

    def answer_of(u: _Unit) -> Optional[str]:
        m = _ANSWER_WORD.search(norm[u.start:u.end])
        if not m:
            return None
        return "affirmative" if m.group(1) is not None else "deny"

    def status_only(u: _Unit) -> list[str]:
        s = norm[u.start:u.end]
        if _MARK in s:
            return []
        keys: list[str] = []
        if _STATUS_ONLY_CITIZEN.search(s):
            keys.append("citizen")
        if _STATUS_ONLY_PR.search(s):
            keys.append("pr")
        if _STATUS_ONLY_PERMIT.search(s):
            keys.append("permit")
        if _SPONSOR_NEED.search(s) and not _SPONSOR_NEGATED.search(s):
            keys.append("sponsored")
        if _DENIAL_ONLY.search(s):
            keys.append("deny")
        return keys

    def list_of(u: _Unit) -> list[str]:
        m = _PURE_LIST.search(norm[u.start:u.end])
        return codes_at(u.start + m.start(1), u.start + m.end(1)) if m else []

    def try_pair(lst: _Unit, other: _Unit) -> bool:
        codes = list_of(lst)
        if not codes:
            return False
        answer = answer_of(other)
        keys = [answer] if answer else status_only(other)
        if not keys:
            return False
        for k in keys:
            bind(codes, k)
        return True

    for i in range(len(units) - 1):
        if i in paired or not _WEAK_BREAK.search(units[i].break_after):
            continue
        a, b = units[i], units[i + 1]
        if try_pair(b, a) or try_pair(a, b):
            paired.add(i)
            paired.add(i + 1)

    # A status-only unit in brackets after one country: "Authorized to work in Canada (citizen)",
    # "Canada: yes (citizen)". A list before it is ambiguous and binds nothing.
    for i in range(1, len(units)):
        if i in paired or _trim(units[i - 1].break_after) != "(" or not _CLOSING_BRACKET.search(units[i].break_after):
            continue
        keys = [k for k in status_only(units[i]) if k in ("citizen", "pr", "permit")]
        if not keys:
            continue
        prev = units[i - 1]
        m = _SINGLE_COUNTRY_AT_END.search(norm[prev.start:prev.end])
        if not m:
            continue
        codes = codes_at(prev.start + m.start(1), prev.start + m.end(1))
        if len(codes) != 1:
            continue
        for k in keys:
            bind(codes, k)
        paired.add(i)

    for i, u in enumerate(units):
        if i in paired:
            continue
        s = norm[u.start:u.end]
        before = unmasked[u.start:u.end]
        # Needs that bind a country of their own, said with no country.
        negated_need = bool(_SPONSOR_NEGATED.search(before))
        if not negated_need and _LMIA_NEED.search(before):
            bind(["CA"], "sponsored")
        if not negated_need and _SPONSORED_VISA_NEED.search(before):
            bind(["US"], "sponsored")
        # A unit with no country binds nothing more: only a no-country sponsorship need is left.
        if _MARK not in s:
            if not _SPONSOR_NEGATED.search(s) and _SPONSOR_NEED.search(s):
                sponsorship_no_country = True
            continue
        # "Canadian", "I'm Canadian", "American living in Toronto".
        dm = _DEMONYM_UNIT.search(s)
        if dm:
            name = names.get(u.start + dm.start(1))
            if name and name[1] in _DEMONYMS and len(name[1]) == len(dm.group(1)):
                bind([name[0]], "citizen")
        # Countries and punctuation alone: every pattern below needs a word or an answer mark.
        if not _WORD_OR_MARK.search(_MARK_RUN.sub("", s)):
            continue
        if not _INTENT_WORDS.search(s):
            for m in _PERMIT_WITH.finditer(s):
                bind(codes_at(u.start + m.start(1), u.start + m.end(1)), "permit")

        def list_codes(m: re.Match[str], group: int, u: _Unit = u) -> list[str]:
            return codes_at(u.start + m.start(group), u.start + m.end(group))

        nxt = units[i + 1] if i + 1 < len(units) else None
        answered_no = bool(_QUESTION.search(u.break_after)) and nxt is not None and answer_of(nxt) == "deny"
        used: list[tuple[int, int]] = []
        for pattern, key in _POSTFIX_PATTERNS:
            for m in pattern.finditer(s):
                used.append((m.start() + len(m.group(1)), m.end()))
                bind(list_codes(m, 1), key)
        for pattern, key in _PREFIX_ANSWER_PATTERNS:
            for m in pattern.finditer(s):
                start = m.start()
                answer_end = start + len(m.group(0)) - len(m.group(1))
                if any(start < q and p < answer_end for p, q in used):
                    continue
                bind(list_codes(m, 1), key)
        negated_sponsor = bool(_SPONSOR_NEGATED.search(s))
        sponsor_bound = False
        for pattern, key in _PATTERNS:
            if key == "sponsored" and negated_sponsor:
                continue
            for m in pattern.finditer(s):
                # "PR in Canada", but not "US permanent resident in Canada".
                if (key == "pr" and _PR_AFTER.search(m.group(0))
                        and _LIST_AT_END.search(s[max(0, m.start() - 40):m.start()])):
                    continue
                flipped = "deny" if answered_no and key == "affirmative" else key
                if key == "sponsored":
                    sponsor_bound = True
                bind(list_codes(m, 1), flipped)
        for m in _NOT_PLACE.finditer(s):
            codes = list_codes(m, 2)
            first = names.get(u.start + m.start(2))
            first_key = first[1] if first else ""
            about_citizenship = m.group(1) is not None or m.group(3) is not None or first_key in _DEMONYMS
            bind(codes, "notCitizen" if about_citizenship else "deny")
        if not sponsor_bound and not negated_sponsor and _SPONSOR_NEED.search(s):
            sponsorship_no_country = True
    return _Reading(bound, mentioned, sponsorship_no_country)


def work_auth_facts(profile: ProfileInput) -> WorkAuthFacts:
    """Read the free-text work authorization (and the stated sponsorship answer) into per-country
    facts (TS: ``workAuthFacts``), the binding way: a country gets a status only from a pattern
    bound to it, and stays unknown otherwise.

    "Canadian citizen"                          CA: authorized, no sponsorship
    "Authorized to work in Canada and the U.S." CA, US: authorized
    "Permanent resident of Canada"              CA: authorized, no sponsorship
    "F-1 student visa (OPT eligible)"           US: authorization unknown (restricted)
    "H-1B"                                      US: authorized, needs sponsorship
    "Yes"                                       unscoped: authorized

    The explicit per-country answers (authorizedUS / authorizedCanada) are applied last.
    """
    text = _normalize_answer(_text(profile, "workAuthorization"))
    by_country: dict[str, CountryAuth] = {}
    raw_sponsorship = _trim(collapse_spaces(_text(profile, "requiresSponsorship")))
    stated_sponsorship = polarity_of(raw_sponsorship)
    # "N/A - citizen" says why no sponsorship applies: unanswered, but not unclear.
    sponsorship_unclear = (raw_sponsorship != "" and stated_sponsorship is None
                           and not _NA_BECAUSE_SETTLED.search(raw_sponsorship.lower()))
    reading = _read_work_auth(text)

    if not reading.mentioned:
        # No country anywhere: the statement is about wherever the applicant is.
        lower = text.lower()
        every = _status_flags_of(_mask_visa_intent(_mask_abbreviations(text)))
        denied = bool(_DENIED_UNSCOPED.search(lower)) or (not every.pr and bool(_NOT_A_CITIZEN.search(lower)))
        # "No sponsorship needed" is not a No to being authorized. "No, I need sponsorship" is: a
        # leading No that a sponsorship need confirms, with nothing that moves it to later.
        if _SPONSOR_WORD.search(lower):
            bare_yes = (False if (_LEADING_NO.search(lower) and _SPONSOR_NEED.search(lower)
                                  and not _SPONSOR_NEGATED.search(lower) and not _NOT_TODAY.search(lower))
                        else None)
        else:
            bare_yes = polarity_of(text)
        unscoped: Optional[bool] = None
        if denied or bare_yes is False:
            unscoped = False
        elif every.affirmative or bare_yes is True or every.citizen or every.pr:
            unscoped = True
        _apply_explicit_countries(profile, by_country)
        return WorkAuthFacts(_ReadOnlyDict(by_country), stated_sponsorship, unscoped, sponsorship_unclear)

    for code, f in reading.bound.items():
        citizen = f.citizen and not f.notCitizen
        auth: Optional[CountryAuth] = None
        if f.deny:
            auth = CountryAuth(False, None, "denied")
        elif citizen and not f.pr and not f.student and not f.permit:
            auth = CountryAuth(True, False, "citizen")
        elif f.pr and not f.student and not f.permit:
            auth = CountryAuth(True, False, "permanent_resident")
        elif f.sponsoredVisa:
            auth = CountryAuth(True, True, "work_permit")
        elif f.student:
            auth = CountryAuth(None, None, "student")
        elif f.permit:
            auth = CountryAuth(True, None, "work_permit")
        elif f.affirmative:
            auth = CountryAuth(True, None, "statement")
        # "I would require sponsorship to work in the US": sponsorship is needed there.
        if f.sponsored and not (auth is not None and auth.basis in _SETTLED):
            auth = CountryAuth(auth.authorized if auth else None, True, auth.basis if auth else "statement")
        if auth is not None:
            by_country[code] = auth
    # "Would need sponsorship" with no country: never where the applicant is a citizen or
    # permanent resident; otherwise every country they name.
    if reading.sponsorship_no_country:
        for code in reading.mentioned:
            auth = by_country.get(code)
            if auth is not None and auth.basis in _SETTLED:
                continue
            by_country[code] = CountryAuth(auth.authorized if auth else None, True, auth.basis if auth else "statement")
    _apply_explicit_countries(profile, by_country)
    return WorkAuthFacts(_ReadOnlyDict(by_country), stated_sponsorship, None, sponsorship_unclear)


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
        # Every US work visa and student status is temporary: a "No" beside OPT or H-1B is not
        # believed, and not contradicted either.
        if stated is False and c is not None and country_code == "US" and c.basis in ("work_permit", "student"):
            return None
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
    if len(auth.by_country) == 1 and not auth.sponsorship_unclear:
        (only,) = auth.by_country.values()
        if only.needs_sponsorship is False:
            return Fact(False, "medium", f"sponsorship:{only.basis}-unscoped")
    return None
